"""Face detection (OpenCV YuNet, MIT-licensed model from opencv_zoo).
Used to enforce avoid=["people's faces"] visually and to keep generated media face-free."""
from pathlib import Path

from app.core.config import get_settings


class YuNetFaces:
    name = "yunet"

    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        self.path = get_settings().face_model_path

    def count(self, image_path: Path, min_rel_size: float = 0.04) -> int:
        img = self.cv2.imread(str(image_path))
        if img is None:
            return 0
        h, w = img.shape[:2]
        det = self.cv2.FaceDetectorYN.create(self.path, "", (w, h), 0.75, 0.3, 50)
        _, faces = det.detect(img)
        if faces is None:
            return 0
        return sum(1 for f in faces if f[2] >= w * min_rel_size)  # ignore tiny background faces


class MockFaces:
    name = "mock"

    def count(self, image_path: Path, min_rel_size: float = 0.04) -> int:
        return 1 if "face" in image_path.name else 0


def get_face_detector():
    p = get_settings().face_detector
    if p == "yunet":
        return YuNetFaces()
    if p == "mock":
        return MockFaces()
    return None
