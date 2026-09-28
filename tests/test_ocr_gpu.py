import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from foodfinder import events


class GPUOCRTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(events, "_READER", None))
        self.enterContext(patch.dict(os.environ, {"FOODFINDER_OCR_DEVICE": "auto", "FOODFINDER_OCR_BATCH_SIZE": "1"}))

    def test_auto_selects_available_intel_gpu_and_cpu_otherwise(self):
        for available, expected in [(True, "xpu"), (False, "cpu")]:
            with self.subTest(available=available):
                torch = SimpleNamespace(xpu=SimpleNamespace(is_available=lambda: available))
                with patch.dict("sys.modules", {"torch": torch}):
                    self.assertEqual(events._ocr_device(), expected)

    def test_cpu_override_does_not_probe_gpu(self):
        torch = SimpleNamespace(xpu=SimpleNamespace(is_available=Mock()))
        with patch.dict(os.environ, {"FOODFINDER_OCR_DEVICE": "cpu"}), patch.dict("sys.modules", {"torch": torch}):
            self.assertEqual(events._ocr_device(), "cpu")
            torch.xpu.is_available.assert_not_called()

    def test_intel_models_are_unquantized_and_both_moved_to_gpu(self):
        reader = SimpleNamespace(detector=Mock(), recognizer=Mock(), device="cpu")
        detector, recognizer = reader.detector, reader.recognizer
        easyocr = SimpleNamespace(Reader=Mock(return_value=reader))
        with patch.dict("sys.modules", {"easyocr": easyocr}):
            self.assertIs(events._build_ocr_reader("xpu"), reader)
        easyocr.Reader.assert_called_once_with(["en"], gpu=False, quantize=False, verbose=False)
        detector.to.assert_called_once_with("xpu")
        recognizer.to.assert_called_once_with("xpu")
        self.assertEqual(reader.device, "xpu")

    def test_gpu_initialization_failure_caches_cpu_fallback(self):
        cpu = SimpleNamespace(device="cpu")
        with patch.object(events, "_ocr_device", return_value="xpu"), patch.object(
            events, "_build_ocr_reader", side_effect=[RuntimeError("driver failure"), cpu]
        ) as build:
            self.assertIs(events._ocr_reader(), cpu)
            self.assertIs(events._ocr_reader(), cpu)
            self.assertEqual([call.args for call in build.call_args_list], [("xpu",), ("cpu",)])

    def test_gpu_failure_retries_same_image_and_uses_cpu_for_remaining_images(self):
        gpu = SimpleNamespace(device="xpu", readtext=Mock(side_effect=RuntimeError("GPU failure")))
        cpu = SimpleNamespace(device="cpu", readtext=Mock(side_effect=[["Free pizza"], ["6 PM"]]))
        with patch.object(events, "_ocr_reader", side_effect=[gpu, cpu]) as get_reader:
            self.assertEqual(events.ocr_images(["poster.jpg", "slide.jpg"]), "Free pizza\n6 PM")
            self.assertEqual(get_reader.call_args_list[-1].kwargs, {"force_cpu": True})
        self.assertEqual([call.args[0] for call in cpu.readtext.call_args_list], ["poster.jpg", "slide.jpg"])

    def test_failed_cpu_retry_preserves_text_from_previous_images(self):
        gpu = SimpleNamespace(device="xpu", readtext=Mock(side_effect=[["Free pizza"], RuntimeError("GPU failure")]))
        cpu = SimpleNamespace(device="cpu", readtext=Mock(side_effect=ValueError("bad image")))
        with patch.object(events, "_ocr_reader", side_effect=[gpu, cpu]):
            with self.assertRaises(events.OCRUnavailableError) as caught:
                events.ocr_images(["poster.jpg", "broken.jpg"])
        self.assertEqual(caught.exception.partial_text, "Free pizza")
