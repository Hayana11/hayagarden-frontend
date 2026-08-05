"""Focused FILE+CHOICES contract regressions."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from chat.attachment_contract import (
    MAX_IMAGE_INPUT_BYTES,
    AttachmentValidationError,
    read_limited_upload,
    reencode_chat_image,
    resolve_uploaded_file_url,
    safe_child_path,
    sandbox_preview_shell,
    validate_uploaded_file_reference,
)
from chat.choices_contract import extract_choices


class ChoicesContractTests(unittest.TestCase):
    def test_first_group_is_extracted_and_later_group_is_preserved(self):
        clean, choices = extract_choices('before [choices] A | B | C [/choices] after [choices]D|E[/choices]')
        self.assertEqual(choices, ['A', 'B', 'C'])
        self.assertIn('[choices]D|E[/choices]', clean)
        self.assertNotIn('[choices] A | B | C [/choices]', clean)

    def test_invalid_or_oversized_items_are_bounded(self):
        raw = '[choices]| A |' + ('x' * 121) + '| B [/choices]'
        clean, choices = extract_choices(raw)
        self.assertEqual(choices, ['A', 'B'])
        self.assertEqual(clean, '')

    def test_invalid_first_group_does_not_scan_valid_second_group(self):
        raw = '[choices]|' + ('x' * 121) + '|[/choices] after [choices]A|B[/choices]'
        clean, choices = extract_choices(raw)
        self.assertEqual(choices, [])
        self.assertEqual(clean, raw)

    def test_astral_unicode_uses_code_point_boundary(self):
        clean, choices = extract_choices('[choices]' + ('😀' * 120) + '[/choices]')
        self.assertEqual(choices, ['😀' * 120])
        self.assertEqual(clean, '')

        raw = '[choices]' + ('😀' * 121) + '[/choices]'
        self.assertEqual(extract_choices(raw), (raw, []))


class AttachmentPathTests(unittest.TestCase):
    def test_url_and_child_path_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            good = base / 'abcd1234_note.md'
            good.write_text('hello', encoding='utf-8')
            url = '/static/uploads/files/abcd1234_note.md'
            self.assertEqual(resolve_uploaded_file_url(url, td), good.resolve())
            self.assertEqual(validate_uploaded_file_reference(url, 'note.md', td)[0], good.resolve())
            self.assertIsNone(resolve_uploaded_file_url('/static/uploads/files/../secret.md', td))
            self.assertIsNone(resolve_uploaded_file_url('/static/uploads/files/%2e%2e%2fsecret.md', td))
            self.assertIsNone(resolve_uploaded_file_url('/etc/passwd', td))
            self.assertIsNone(safe_child_path(td, '..\\secret.md'))
            self.assertIsNone(validate_uploaded_file_reference(url, 'note.html', td))
            self.assertIsNone(validate_uploaded_file_reference(url, '[choices]x[/choices].md', td))


class HtmlSandboxTests(unittest.TestCase):
    def test_shell_uses_script_only_opaque_origin_sandbox(self):
        shell = sandbox_preview_shell('/api/artifacts/7/content')
        self.assertIn('sandbox="allow-scripts"', shell)
        self.assertNotIn('allow-same-origin', shell)
        self.assertIn(".srcdoc=String(data.content||'')", shell)
        self.assertNotIn('window.parent', shell)


class ImageContractTests(unittest.TestCase):
    @staticmethod
    def _image_bytes(fmt: str, size=(48, 32), mode='RGB') -> bytes:
        from PIL import Image
        image = Image.new(mode, size, (120, 30, 60, 128) if 'A' in mode else (120, 30, 60))
        out = io.BytesIO()
        image.save(out, format=fmt)
        return out.getvalue()

    def test_normal_jpeg_png_webp_are_decoded_and_reencoded(self):
        cases = [('JPEG', '.jpg', 'image/jpeg'), ('PNG', '.png', 'image/png'), ('WEBP', '.webp', 'image/webp')]
        for fmt, ext, mime in cases:
            with self.subTest(fmt=fmt):
                encoded, actual_ext, actual_mime = reencode_chat_image(self._image_bytes(fmt))
                self.assertEqual((actual_ext, actual_mime), (ext, mime))
                self.assertTrue(encoded)
                self.assertNotEqual(encoded, self._image_bytes(fmt))

    def test_disguised_text_and_oversized_bytes_are_rejected(self):
        with self.assertRaises(AttachmentValidationError):
            reencode_chat_image(b'not actually a jpeg')
        with self.assertRaises(AttachmentValidationError) as caught:
            read_limited_upload(io.BytesIO(b'x' * (MAX_IMAGE_INPUT_BYTES + 1)))
        self.assertEqual(caught.exception.status, 413)

    def test_excessive_dimensions_are_rejected_before_persistence(self):
        data = self._image_bytes('PNG', size=(8193, 1))
        with self.assertRaises(AttachmentValidationError) as caught:
            reencode_chat_image(data)
        self.assertEqual(caught.exception.status, 413)


if __name__ == '__main__':
    unittest.main()
