import os
import pathlib
from pathlib import Path
from typing import Tuple, Optional
import cv2
import torch
import yolov5

if os.name == 'nt':
    pathlib.PosixPath = pathlib.WindowsPath

_MODEL_3D = None

def get_3d_model():
    global _MODEL_3D
    if _MODEL_3D is None:
        weight_path = Path(__file__).parent / "weight" / "3d_captcha.pt"
        if not weight_path.exists():
            weight_path = Path("api-solve-captcha/api-solve-captcha/tiktok_captcha/weight/3d_captcha.pt")
        _orig_load = torch.load
        try:
            torch.load = lambda *a, **kw: _orig_load(*a, **{**kw, 'weights_only': False})
            _MODEL_3D = yolov5.load(str(weight_path))
        finally:
            torch.load = _orig_load
    return _MODEL_3D


def solve_3d_captcha_tiktok(image_path: str) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """
    Solves the 3D captcha by finding two objects with the same label and returning their center coordinates (x/w, y/h).

    Args:
        image_path: Path to the image file.

    Returns:
        A tuple containing two tuples, representing normalized center coordinates of two objects with the same label.
    """
    try:
        model = get_3d_model()
        img = cv2.imread(str(image_path))
        if img is None:
            return None
        height, width, _ = img.shape
        results = model(str(image_path))
        predictions = results.pred[0]

        if len(predictions) < 2:
            return None  # Not enough objects to find a pair

        boxes = predictions[:, :4]  # x1, y1, x2, y2
        categories = predictions[:, 5].int()  # Convert to integer labels

        # Find two objects with the same label
        for i in range(len(categories)):
            for j in range(i + 1, len(categories)):
                if categories[i] == categories[j]:
                    c1 = ((float(boxes[i][0]) + float(boxes[i][2])) / 2.0, (float(boxes[i][1]) + float(boxes[i][3])) / 2.0)
                    c2 = ((float(boxes[j][0]) + float(boxes[j][2])) / 2.0, (float(boxes[j][1]) + float(boxes[j][3])) / 2.0)
                    center1 = (c1[0] / width, c1[1] / height)
                    center2 = (c2[0] / width, c2[1] / height)
                    return center1, center2

        return None  # No matching objects found

    except Exception as e:
        print(f"Error processing image: {e}")
        return None
    
if __name__ == '__main__':
    image_path = '3d_captcha/captcha_image_2.png'
    
    centers = solve_3d_captcha_tiktok(image_path)

    if centers:
        center1, center2 = centers
        print(f"Center 1: {center1}")
        print(f"Center 2: {center2}")
    else:
        print("No matching objects found.")
    