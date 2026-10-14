import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from parity.core.matching import (  # noqa: E402
    match_photo_hashes,
    match_photo_keypoints,
    phash_image,
)


def _scene(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((600, 900, 3), 235, np.uint8)
    for _ in range(140):
        x, y = int(rng.integers(0, 860)), int(rng.integers(0, 560))
        w, h = int(rng.integers(15, 120)), int(rng.integers(15, 120))
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            cv2.rectangle(image, (x, y), (x + w, y + h), colour, -1)
        else:
            cv2.circle(image, (x, y), w // 2, colour, -1)
    return image


def _jpeg(image: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


def _channel_variant(image: np.ndarray) -> np.ndarray:
    """What an OTA might publish: cropped off-centre, resized, banner added."""
    cropped = image[90:560, 160:880]
    resized = cv2.resize(cropped, (640, 418))
    cv2.rectangle(resized, (0, 0), (640, 40), (20, 20, 20), -1)
    return resized


def test_keypoints_match_cropped_channel_photos_that_phash_misses():
    originals = [_scene(seed) for seed in (1, 2, 3)]
    subject = [_jpeg(image) for image in originals]
    candidate = [_jpeg(_channel_variant(image)) for image in originals]

    phash = match_photo_hashes([phash_image(x) for x in subject], [phash_image(x) for x in candidate])
    keypoints = match_photo_keypoints(subject, candidate)

    assert phash.matching_pairs < 2
    assert keypoints.same_property
    assert keypoints.matching_pairs == 3


def test_keypoints_do_not_match_a_different_property():
    subject = [_jpeg(_scene(seed)) for seed in (1, 2, 3)]
    different = [_jpeg(_channel_variant(_scene(seed))) for seed in (11, 12, 13)]
    summary = match_photo_keypoints(subject, different)
    assert summary.matching_pairs == 0
    assert not summary.same_property


def test_keypoints_ignore_undecodable_bytes():
    summary = match_photo_keypoints([b"not an image"], [_jpeg(_scene(1))])
    assert summary.matching_pairs == 0
