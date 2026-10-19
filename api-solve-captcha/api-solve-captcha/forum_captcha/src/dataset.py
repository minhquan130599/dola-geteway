import os
import glob

import torch
from torch.utils.data import Dataset
from scipy import signal
from scipy.io import wavfile
from PIL import Image
import numpy as np


class CustomDataset(Dataset):
    CHARS = '0123456789abcdefghijklmnopqrstuvwxyz'
    CHAR2LABEL = {char: i + 1 for i, char in enumerate(CHARS)}
    LABEL2CHAR = {label: char for char, label in CHAR2LABEL.items()}

    def __init__(self, root_dir=None, mode=None, paths=None, img_height=32, img_width=100):
        if root_dir and mode and not paths:
            paths, texts = self.load_data(root_dir, mode)
        elif not root_dir and not mode and paths:
            texts = None

        self.paths = paths
        self.texts = texts
        self.img_height = img_height
        self.img_width = img_width
        
        
    def load_data(self, root_dir, mode):
        if mode == 'train':
            label_file = os.path.join(root_dir, 'train.txt')
        elif mode == 'dev':
            label_file = os.path.join(root_dir, 'val.txt')
        image_folder = os.path.join(root_dir, 'images')
        image_paths = []
        labels = []

        with open(label_file, 'r') as f:
            lines = f.readlines()
            for line in lines:
                img_path, label = line.strip().split()  # Chia đường dẫn ảnh và nhãn
                image_paths.append(os.path.join(image_folder, img_path))  # Tạo đường dẫn tuyệt đối tới hình ảnh
                labels.append(label)  # Lưu nhãn nguyên thủy

        return image_paths, labels

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, index):
        path = self.paths[index]

        try:
            image = Image.open(path).convert('L')  # grey-scale
        except IOError:
            print('Corrupted image for %d' % index)
            return self[index + 1]

        image = image.resize((self.img_width, self.img_height), resample=Image.BILINEAR)
        image = np.array(image)
        image = image.reshape((1, self.img_height, self.img_width))
        image = (image / 127.5) - 1.0

        image = torch.FloatTensor(image)
        if self.texts:
            text = self.texts[index]
            target = [self.CHAR2LABEL[c] for c in text]
            target_length = [len(target)]

            target = torch.LongTensor(target)
            target_length = torch.LongTensor(target_length)
            return image, target, target_length
        else:
            return image


def custom_collate_fn(batch):
    images, targets, target_lengths = zip(*batch)
    images = torch.stack(images, 0)
    targets = torch.cat(targets, 0)
    target_lengths = torch.cat(target_lengths, 0)
    return images, targets, target_lengths


