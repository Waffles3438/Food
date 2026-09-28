import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from foodfinder import events


class OCRBatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"FOODFINDER_OCR_BATCH_SIZE": "4"}))

    def test_gpu_batches_are_bounded_and_preserve_order_including_remainder(self):
        reader = SimpleNamespace(device="xpu", readtext=Mock(return_value=["image8"]))
        paths = [f"image{i}" for i in range(9)]
        with patch.object(events, "_ocr_reader", return_value=reader), patch.object(
            events, "_read_image_batch", side_effect=lambda reader, chunk: [[path] for path in chunk]
        ) as batch:
            self.assertEqual(events.ocr_images(paths), "\n".join(paths))
        self.assertEqual([len(call.args[1]) for call in batch.call_args_list], [4, 4])
        reader.readtext.assert_called_once_with("image8", detail=0)

    def test_cpu_keeps_single_image_processing(self):
        reader = SimpleNamespace(device="cpu", readtext=Mock(side_effect=[["first"], ["second"]]))
        with patch.object(events, "_ocr_reader", return_value=reader), patch.object(events, "_read_image_batch") as batch:
            self.assertEqual(events.ocr_images(["one", "two"]), "first\nsecond")
        batch.assert_not_called()

    def test_failed_batch_retries_all_images_once_in_original_order(self):
        reader = SimpleNamespace(device="xpu", readtext=Mock(side_effect=lambda path, **kwargs: [path]))
        with patch.object(events, "_ocr_reader", return_value=reader), patch.object(
            events, "_read_image_batch", side_effect=RuntimeError("batch memory exhausted")
        ) as batch:
            self.assertEqual(events.ocr_images(["a", "b", "c", "d", "e"]), "a\nb\nc\nd\ne")
        batch.assert_called_once()
        self.assertEqual([call.args[0] for call in reader.readtext.call_args_list], ["a", "b", "c", "d", "e"])

    def test_broken_image_preserves_other_text_after_batch_failure(self):
        gpu = SimpleNamespace(device="xpu", readtext=Mock(side_effect=[["first"], ValueError("bad image")]))
        cpu = SimpleNamespace(device="cpu", readtext=Mock(side_effect=[ValueError("bad image"), ["last"]]))
        with patch.object(events, "_ocr_reader", side_effect=[gpu, cpu]), patch.object(
            events, "_read_image_batch", side_effect=ValueError("bad image")
        ):
            with self.assertRaises(events.OCRUnavailableError) as error:
                events.ocr_images(["first", "broken", "last"])
        self.assertEqual(error.exception.partial_text, "first\nlast")

    def test_mixed_sizes_are_padded_without_distortion_and_greys_preserved(self):
        first = (np.full((2, 3, 3), 20, dtype=np.uint8), np.full((2, 3), 30, dtype=np.uint8))
        second = (np.full((4, 2, 3), 40, dtype=np.uint8), np.full((4, 2), 50, dtype=np.uint8))
        reader = SimpleNamespace(detect=Mock(return_value=([[], []], [[], []])), recognize=Mock(side_effect=[["first"], ["second"]]))
        with patch("easyocr.utils.reformat_input", side_effect=[first, second]):
            self.assertEqual(events._read_image_batch(reader, ["one", "two"]), [["first"], ["second"]])
        colors = reader.detect.call_args.args[0]
        self.assertEqual(colors.shape, (2, 4, 3, 3))
        np.testing.assert_array_equal(colors[0, :2, :3], first[0])
        self.assertTrue((colors[0, 2:] == 255).all())
        self.assertTrue((colors[1, :, 2:] == 255).all())
        np.testing.assert_array_equal(reader.recognize.call_args_list[0].args[0][:2, :3], first[1])

    def test_oversized_padded_batch_is_rejected_before_gpu_allocation(self):
        image = SimpleNamespace(shape=(4096, 4096, 3))
        reader = SimpleNamespace(detect=Mock())
        with patch("easyocr.utils.reformat_input", return_value=(image, image)):
            with self.assertRaisesRegex(ValueError, "pixel budget"):
                events._read_image_batch(reader, ["one", "two"])
        reader.detect.assert_not_called()

    def test_invalid_batch_sizes_are_rejected(self):
        for value in ("0", "-1", "17", "four"):
            with self.subTest(value=value), patch.dict(os.environ, {"FOODFINDER_OCR_BATCH_SIZE": value}):
                with self.assertRaises(ValueError):
                    events._ocr_batch_size()
