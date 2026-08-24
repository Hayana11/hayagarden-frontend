"""Focused FILE+CHOICES contract regressions."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from chat.attachment_contract import (
    ALLOWED_CHAT_FILE_EXTENSIONS,
    MAX_CHAT_ATTACHMENTS,
    MAX_IMAGE_INPUT_BYTES,
    MAX_TEXT_FILE_BYTES,
    AttachmentValidationError,
    read_limited_upload,
    render_markdown_preview_page,
    reencode_chat_image,
    resolve_uploaded_file_url,
    safe_child_path,
    sandbox_preview_shell,
    validate_uploaded_file_reference,
    write_limited_text_upload,
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


class MultiAttachmentContractTests(unittest.TestCase):
    def test_word_pdf_and_four_attachment_limit_are_explicit(self):
        self.assertEqual(MAX_CHAT_ATTACHMENTS, 4)
        self.assertTrue({'.doc', '.docx', '.pdf'}.issubset(ALLOWED_CHAT_FILE_EXTENSIONS))

    def test_word_and_pdf_references_keep_path_and_size_validation(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for suffix in ('.doc', '.docx', '.pdf'):
                name = 'abcd1234_note' + suffix
                path = base / name
                path.write_bytes(b'not parsed as text')
                result = validate_uploaded_file_reference(
                    '/static/uploads/files/' + name,
                    'note' + suffix,
                    td,
                )
                self.assertIsNotNone(result)
                self.assertEqual(result[0], path.resolve())


class ChatMultiAttachmentRouteTests(unittest.TestCase):
    def test_send_route_uses_durable_attachment_array_and_four_item_gate(self):
        source = (Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8')
        start = source.index("def send_chat():")
        end = source.index("@app.route", start + 1)
        route = source[start:end]
        self.assertIn("request.files.getlist('image')", route)
        self.assertIn("len(attachments) > MAX_CHAT_ATTACHMENTS", route)
        self.assertIn("attachments_json = json.dumps(attachments, ensure_ascii=False)", route)
        self.assertIn("file_name,attachments", route)
        rewrite_source = (Path(__file__).parents[1] / 'chat' / 'rewrite_staging.py').read_text(
            encoding='utf-8'
        )
        self.assertIn("('attachments', attachments)", rewrite_source)


class ArtifactSandboxTests(unittest.TestCase):
    def test_shell_uses_script_only_opaque_origin_sandbox(self):
        shell = sandbox_preview_shell('/api/artifacts/7/content')
        self.assertIn('sandbox="allow-scripts"', shell)
        self.assertNotIn('allow-same-origin', shell)
        self.assertIn(".srcdoc=String(data.content||'')", shell)
        self.assertNotIn('window.parent', shell)

    def test_markdown_heading_table_and_code_render_inside_preview_page(self):
        page = render_markdown_preview_page(
            'Markdown preview',
            '# Heading\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nprint(1)\n```',
        )
        self.assertIn('<h1>Heading</h1>', page)
        self.assertIn('<table>', page)
        self.assertIn('<pre><code class="language-python">', page)

    def test_markdown_raw_script_is_delivered_only_to_opaque_sandbox_srcdoc(self):
        page = render_markdown_preview_page(
            'raw script',
            '<script>parent.document.body.dataset.pwned = document.cookie;</script>',
        )
        self.assertIn('<script>parent.document.body.dataset.pwned = document.cookie;</script>', page)
        shell = sandbox_preview_shell('/api/artifacts/8/content')
        self.assertIn('sandbox="allow-scripts"', shell)
        self.assertNotIn('allow-same-origin', shell)
        self.assertIn(".srcdoc=String(data.content||'')", shell)

    def test_html_and_markdown_artifacts_share_the_existing_sandbox_route(self):
        source = (Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8')
        preview = source[source.index('def artifact_preview'):source.index("@app.route('/api/artifacts/<int:aid>/content'")]
        content = source[source.index('def artifact_html_content'):source.index('def _sandbox_preview_shell')]
        self.assertIn("meta['type'] in {'html', 'markdown'}", preview)
        self.assertIn("_sandbox_preview_shell('/api/artifacts/%d/content' % aid)", preview)
        self.assertNotIn('Response(page', preview)
        self.assertIn("meta['type'] == 'html'", content)
        self.assertIn("meta['type'] == 'markdown'", content)
        self.assertIn('render_markdown_preview_page', content)


class TextUploadBoundedReadTests(unittest.TestCase):
    def test_text_upload_within_limit_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / 'ok.txt'
            written = write_limited_text_upload(io.BytesIO(b'hello'), destination)
            self.assertEqual(written, 5)
            self.assertEqual(destination.read_bytes(), b'hello')

    def test_text_upload_over_limit_is_413_and_not_written(self):
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / 'too-large.txt'
            with self.assertRaises(AttachmentValidationError) as caught:
                write_limited_text_upload(
                    io.BytesIO(b'x' * (MAX_TEXT_FILE_BYTES + 1)),
                    destination,
                )
            self.assertEqual(caught.exception.status, 413)
            self.assertFalse(destination.exists())

    def test_text_upload_requests_only_upper_bound_plus_one(self):
        class LargeStream:
            def __init__(self):
                self.read_sizes = []

            def read(self, size=-1):
                self.read_sizes.append(size)
                return b'x' * size

        stream = LargeStream()
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / 'bounded.txt'
            with self.assertRaises(AttachmentValidationError) as caught:
                write_limited_text_upload(stream, destination)
            self.assertEqual(caught.exception.status, 413)
            self.assertEqual(stream.read_sizes, [MAX_TEXT_FILE_BYTES + 1])
            self.assertFalse(destination.exists())


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
