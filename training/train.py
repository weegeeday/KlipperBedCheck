#!/usr/bin/env python3
"""Train a tiny CNN for Klipper Bed Clear.

Expected layout:

    data/frames/clear/*.jpg
    data/frames/not_clear/*.jpg

Sequential video frames are deduplicated before splitting. The split is by
contiguous filename blocks, so near-duplicates do not land in both train and
test. This is a single-session fallback; pass --split-json for real sessions.
"""

import argparse
import copy
import json
import pathlib
import random
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from sklearn.metrics import (accuracy_score, confusion_matrix,
                             precision_recall_fscore_support, roc_auc_score)
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}
CLASSES = ('clear', 'not_clear')
ALIASES = {
    'clear': {'clear', 'bedclearframes', 'bed_clear'},
    'not_clear': {'not_clear', 'notclearframes', 'notclear', 'not clear'},
}


def find_class_dir(root, label):
    for path in sorted(pathlib.Path(root).iterdir()):
        if path.is_dir() and path.name.lower() in ALIASES[label]:
            return path
    return None


def image_files(folder):
    return sorted(path for path in pathlib.Path(folder).iterdir()
                  if path.is_file() and path.suffix.lower() in IMG_EXTS)


def fingerprint(path, size=(32, 32)):
    with Image.open(path) as image:
        return np.asarray(image.convert('L').resize(size, Image.BILINEAR),
                          dtype=np.float32) / 255.0


def dedupe(paths, threshold):
    kept = []
    kept_fps = []
    for path in paths:
        fp = fingerprint(path)
        if any(np.mean(np.abs(fp - other)) < threshold for other in kept_fps):
            continue
        kept.append(path)
        kept_fps.append(fp)
    return kept


def split_items(items, val_frac, test_frac):
    count = len(items)
    n_test = int(round(count * test_frac))
    n_val = int(round(count * val_frac))
    if n_test == 0 and count >= 6:
        n_test = 1
    if n_val == 0 and count >= 6:
        n_val = 1
    n_train = count - n_val - n_test
    if n_train < 1 or n_val < 1 or n_test < 1:
        raise ValueError("not enough unique images for a three-way split")
    return items[:n_train], items[n_train:n_train + n_val], \
        items[n_train + n_val:]


def parse_roi(value):
    parts = [float(v.strip()) for v in value.split(',')]
    if (len(parts) != 4 or not 0. <= parts[0] < parts[2] <= 1.
            or not 0. <= parts[1] < parts[3] <= 1.):
        raise argparse.ArgumentTypeError(
            "roi must be x0,y0,x1,y1 with 0 <= x0 < x1 <= 1")
    return tuple(parts)


def crop_roi(image, roi):
    width, height = image.size
    x0 = int(round(roi[0] * width))
    y0 = int(round(roi[1] * height))
    x1 = max(x0 + 1, int(round(roi[2] * width)))
    y1 = max(y0 + 1, int(round(roi[3] * height)))
    return image.crop((x0, y0, x1, y1))


class BedDataset(Dataset):
    def __init__(self, items, img_size, roi, train):
        self.items = items
        self.roi = roi
        if train:
            self.transform = transforms.Compose([
                transforms.Resize((img_size, img_size)),
                transforms.RandomAffine(degrees=1.5,
                                        translate=(0.02, 0.02),
                                        scale=(0.97, 1.03)),
                transforms.ColorJitter(brightness=0.15, contrast=0.15),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ])
        else:
            self.transform = transforms.Compose([
                transforms.Resize((img_size, img_size)),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ])

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        path, label = self.items[index]
        with Image.open(path) as image:
            image = crop_roi(image.convert('L'), self.roi)
            image = self.transform(image)
        return image, label, str(path)


class SmallCNN(nn.Module):
    def __init__(self, width=16, num_classes=2):
        super().__init__()

        def block(cin, cout):
            return nn.Sequential(
                nn.Conv2d(cin, cout, 3, padding=1, bias=False),
                nn.BatchNorm2d(cout),
                nn.ReLU(inplace=True),
                nn.Conv2d(cout, cout, 3, padding=1, bias=False),
                nn.BatchNorm2d(cout),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
            )

        self.features = nn.Sequential(
            block(1, width),
            block(width, width * 2),
            block(width * 2, width * 4),
            block(width * 4, width * 4),
            nn.AdaptiveAvgPool2d(1),
        )
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Dropout(0.2),
            nn.Linear(width * 4, num_classes),
        )

    def forward(self, x):
        return self.classifier(self.features(x))


def evaluate(model, loader, criterion, device):
    model.eval()
    probabilities = []
    labels = []
    total_loss = 0.
    total = 0
    with torch.no_grad():
        for images, targets, _ in loader:
            images = images.to(device)
            targets = targets.to(device)
            logits = model(images)
            loss = criterion(logits, targets)
            total_loss += loss.item() * images.size(0)
            total += images.size(0)
            probabilities.append(
                torch.softmax(logits, dim=1)[:, 1].cpu().numpy())
            labels.append(targets.cpu().numpy())
    return (np.concatenate(probabilities), np.concatenate(labels),
            total_loss / max(1, total))


def metrics_at(y_true, probabilities, threshold):
    predicted = (probabilities >= threshold).astype(int)
    matrix = confusion_matrix(y_true, predicted, labels=[0, 1])
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, predicted, labels=[0, 1], zero_division=0)
    try:
        auc = roc_auc_score(y_true, probabilities)
    except ValueError:
        auc = float('nan')
    return {
        'threshold': float(threshold),
        'accuracy': float(accuracy_score(y_true, predicted)),
        'not_clear_precision': float(precision[1]),
        'not_clear_recall': float(recall[1]),
        'not_clear_f1': float(f1[1]),
        'clear_precision': float(precision[0]),
        'clear_recall': float(recall[0]),
        'roc_auc': float(auc),
        'confusion_matrix': matrix.tolist(),
        'counts': {'clear': int((y_true == 0).sum()),
                   'not_clear': int((y_true == 1).sum())},
    }


def choose_threshold(y_true, probabilities, target_recall):
    for threshold in np.arange(0.99, 0.049, -0.01):
        predicted = (probabilities >= threshold).astype(int)
        true_positive = int(((predicted == 1) & (y_true == 1)).sum())
        false_negative = int(((predicted == 0) & (y_true == 1)).sum())
        recall = true_positive / max(1, true_positive + false_negative)
        if recall >= target_recall:
            return float(round(threshold, 2))
    return 0.05


def save_confusion(matrix, path):
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.imshow(matrix, cmap='Blues')
    ax.set_xticks([0, 1])
    ax.set_yticks([0, 1])
    ax.set_xticklabels(['clear', 'not clear'])
    ax.set_yticklabels(['clear', 'not clear'])
    ax.set_xlabel('predicted')
    ax.set_ylabel('true')
    for row in range(2):
        for column in range(2):
            ax.text(column, row, str(matrix[row][column]),
                    ha='center', va='center')
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def save_samples(model, items, dataset, device, threshold, path):
    model.eval()
    count = min(9, len(items))
    if count == 0:
        return
    figure, axes = plt.subplots(3, 3, figsize=(9, 9))
    axes = axes.ravel()
    for index in range(9):
        axes[index].axis('off')
    with torch.no_grad():
        for index in range(count):
            image, label, _ = dataset[index]
            logits = model(image.unsqueeze(0).to(device))
            probability = torch.softmax(logits, dim=1)[0, 1].item()
            prediction = 1 if probability >= threshold else 0
            shown = image.squeeze().cpu().numpy() * 0.5 + 0.5
            axes[index].imshow(shown, cmap='gray')
            axes[index].set_title(
                'true %s / pred %s\np(not clear)=%.2f'
                % (CLASSES[label], CLASSES[prediction], probability),
                fontsize=9)
            axes[index].axis('off')
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)


def export_onnx(model, path, img_size):
    export_model = copy.deepcopy(model).cpu().eval()
    dummy = torch.randn(1, 1, img_size, img_size)
    kwargs = {
        'input_names': ['image'],
        'output_names': ['logits'],
        'dynamic_axes': {'image': {0: 'batch'}, 'logits': {0: 'batch'}},
        'opset_version': 12,
    }
    try:
        try:
            # The legacy exporter works without onnxscript. Newer PyTorch
            # defaults to the dynamo exporter when this argument is omitted.
            torch.onnx.export(export_model, dummy, str(path), dynamo=False,
                              **kwargs)
        except TypeError:
            torch.onnx.export(export_model, dummy, str(path), **kwargs)
    except Exception as e:
        print("ONNX export failed: %s" % (e,))
        print("The PyTorch checkpoint and config are still available.")
        return False
    print("Exported %s" % (path,))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/frames')
    parser.add_argument('--out', default='training/runs/bedclear')
    parser.add_argument('--img-size', type=int, default=128)
    parser.add_argument('--epochs', type=int, default=40)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--weight-decay', type=float, default=1e-4)
    parser.add_argument('--patience', type=int, default=10)
    parser.add_argument('--val-frac', type=float, default=0.15)
    parser.add_argument('--test-frac', type=float, default=0.15)
    parser.add_argument('--dedupe-threshold', type=float, default=0.005)
    parser.add_argument('--no-dedupe', action='store_true')
    parser.add_argument('--roi', type=parse_roi, default=(0., 0., 1., 1.))
    parser.add_argument('--width', type=int, default=16)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument('--target-recall', type=float, default=0.98)
    parser.add_argument('--min-unique', type=int, default=20)
    parser.add_argument('--force', action='store_true',
                        help='train even with fewer unique images')
    parser.add_argument('--no-onnx', action='store_true')
    parser.add_argument('--device', default='auto',
                        choices=['auto', 'cpu', 'cuda'])
    args = parser.parse_args()

    if not 0. < args.val_frac < 1. or not 0. < args.test_frac < 1.:
        parser.error('val-frac and test-frac must be between 0 and 1')
    if args.val_frac + args.test_frac >= 1.:
        parser.error('val-frac + test-frac must be less than 1')

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    root = pathlib.Path(args.data)
    if not root.is_dir():
        print("Data directory not found: %s" % (root,), file=sys.stderr)
        return 1

    items_by_class = {}
    for label_index, label in enumerate(CLASSES):
        folder = find_class_dir(root, label)
        if folder is None:
            print("Missing %s folder in %s" % (label, root), file=sys.stderr)
            return 1
        paths = image_files(folder)
        if not paths:
            print("No images in %s" % (folder,), file=sys.stderr)
            return 1
        if args.no_dedupe:
            unique = paths
        else:
            unique = dedupe(paths, args.dedupe_threshold)
        print("%-9s %4d files -> %4d unique" % (label, len(paths), len(unique)))
        if len(unique) < args.min_unique and not args.force:
            print("ERROR: only %d unique images for %s (minimum %d)."
                  % (len(unique), label, args.min_unique), file=sys.stderr)
            print("Collect more scenes/lighting/object arrangements, or pass "
                  "--force to test the pipeline anyway.", file=sys.stderr)
            return 2
        items_by_class[label] = [(path, label_index) for path in unique]

    splits = {'train': [], 'val': [], 'test': []}
    for label_index, label in enumerate(CLASSES):
        items = items_by_class[label]
        try:
            train, val, test = split_items(
                items, args.val_frac, args.test_frac)
        except ValueError as e:
            print("ERROR: %s (%s)" % (e, label), file=sys.stderr)
            return 2
        splits['train'].extend(train)
        splits['val'].extend(val)
        splits['test'].extend(test)

    for name in ('train', 'val', 'test'):
        random.shuffle(splits[name])
        counts = [sum(1 for _, label in splits[name] if label == i)
                  for i in range(2)]
        print("  %-5s %4d images (clear %d, not_clear %d)"
              % (name, len(splits[name]), counts[0], counts[1]))

    if args.device == 'auto':
        device_name = 'cuda' if torch.cuda.is_available() else 'cpu'
    else:
        device_name = args.device
    device = torch.device(device_name)
    print("Device: %s" % (device,))

    train_dataset = BedDataset(splits['train'], args.img_size, args.roi, True)
    val_dataset = BedDataset(splits['val'], args.img_size, args.roi, False)
    test_dataset = BedDataset(splits['test'], args.img_size, args.roi, False)
    loader_args = {'batch_size': args.batch_size, 'num_workers': args.workers,
                   'pin_memory': device.type == 'cuda'}
    train_loader = DataLoader(train_dataset, shuffle=True, **loader_args)
    val_loader = DataLoader(val_dataset, shuffle=False, **loader_args)
    test_loader = DataLoader(test_dataset, shuffle=False, **loader_args)

    model = SmallCNN(width=args.width).to(device)
    parameter_count = sum(p.numel() for p in model.parameters())
    print("Model parameters: %d" % (parameter_count,))

    train_counts = [sum(1 for _, label in splits['train'] if label == i)
                    for i in range(2)]
    total = sum(train_counts)
    class_weights = torch.tensor(
        [total / (2. * max(1, count)) for count in train_counts],
        dtype=torch.float32, device=device)
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr,
                                  weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, args.epochs))

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    best_path = out_dir / 'best.pt'
    best_loss = float('inf')
    stale = 0
    history = []

    for epoch in range(1, args.epochs + 1):
        model.train()
        running_loss = 0.
        seen = 0
        for images, targets, _ in train_loader:
            images = images.to(device)
            targets = targets.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = criterion(logits, targets)
            loss.backward()
            optimizer.step()
            running_loss += loss.item() * images.size(0)
            seen += images.size(0)
        scheduler.step()
        train_loss = running_loss / max(1, seen)
        val_probs, val_labels, val_loss = evaluate(
            model, val_loader, criterion, device)
        val_metrics = metrics_at(val_labels, val_probs, args.threshold)
        history.append({'epoch': epoch, 'train_loss': train_loss,
                        'val_loss': val_loss, **val_metrics})
        print("epoch %3d  train %.4f  val %.4f  val_acc %.3f  "
              "not_clear_recall %.3f"
              % (epoch, train_loss, val_loss, val_metrics['accuracy'],
                 val_metrics['not_clear_recall']))
        if val_loss < best_loss:
            best_loss = val_loss
            stale = 0
            torch.save(model.state_dict(), best_path)
        else:
            stale += 1
            if stale >= args.patience:
                print("Early stop at epoch %d" % (epoch,))
                break

    state_dict = torch.load(best_path, map_location=device,
                            weights_only=True)
    model.load_state_dict(state_dict)
    model.eval()

    val_probs, val_labels, _ = evaluate(model, val_loader, criterion, device)
    recommended = choose_threshold(val_labels, val_probs,
                                   args.target_recall)
    test_probs, test_labels, test_loss = evaluate(
        model, test_loader, criterion, device)
    test_default = metrics_at(test_labels, test_probs, args.threshold)
    test_recommended = metrics_at(test_labels, test_probs, recommended)

    save_confusion(np.array(test_recommended['confusion_matrix']),
                   out_dir / 'confusion_matrix.png')
    save_samples(model, splits['test'], test_dataset, device, recommended,
                 out_dir / 'samples.png')

    config = {
        'classes': list(CLASSES),
        'image': {
            'size': [args.img_size, args.img_size],
            'roi': list(args.roi),
            'channels': 1,
            'normalize_mean': [0.5],
            'normalize_std': [0.5],
        },
        'model': {'type': 'SmallCNN', 'width': args.width,
                  'parameters': parameter_count},
        'threshold': recommended,
        'target_recall': args.target_recall,
    }
    with open(out_dir / 'config.json', 'w') as f:
        json.dump(config, f, indent=2, sort_keys=True)
    with open(out_dir / 'metrics.json', 'w') as f:
        json.dump({
            'data': str(root),
            'dedupe_threshold': (None if args.no_dedupe
                                 else args.dedupe_threshold),
            'splits': {name: len(items) for name, items in splits.items()},
            'validation': metrics_at(val_labels, val_probs, recommended),
            'test_at_0.5': test_default,
            'test_at_recommended': test_recommended,
            'test_loss': test_loss,
            'recommended_threshold': recommended,
            'parameter_count': parameter_count,
            'history': history,
        }, f, indent=2, sort_keys=True)

    if not args.no_onnx:
        export_onnx(model, out_dir / 'model.onnx', args.img_size)

    print()
    print("Test at 0.5:              accuracy %.3f  not_clear recall %.3f"
          % (test_default['accuracy'], test_default['not_clear_recall']))
    print("Test at recommended %.2f: accuracy %.3f  not_clear recall %.3f"
          % (recommended, test_recommended['accuracy'],
             test_recommended['not_clear_recall']))
    print("Artifacts: %s" % (out_dir,))
    return 0


if __name__ == '__main__':
    sys.exit(main())
