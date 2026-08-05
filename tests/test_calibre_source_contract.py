import ast
import os
import unittest
from pathlib import Path


SOURCE_ROOT = Path(os.environ.get("CALIBRE_SOURCE_ROOT", "/tmp/calibre-source/src"))


@unittest.skipUnless(SOURCE_ROOT.is_dir(), "Calibre source tree is not available")
class CalibreSourceContractTests(unittest.TestCase):
    @staticmethod
    def _argument_names(node):
        return [argument.arg for argument in node.args.args]

    @classmethod
    def module_ast(cls, relative_path):
        path = SOURCE_ROOT / relative_path
        if not path.is_file():
            raise AssertionError(f"Expected source file not found: {relative_path}")
        return ast.parse(path.read_text(encoding="utf-8"))

    @classmethod
    def top_level_functions(cls, relative_path):
        tree = cls.module_ast(relative_path)
        return {
            node.name: cls._argument_names(node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    @classmethod
    def class_methods(cls, relative_path, class_name):
        tree = cls.module_ast(relative_path)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return {
                    child.name: cls._argument_names(child)
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
        raise AssertionError(f"Expected class {class_name!r} in {relative_path}")

    def test_cache_mutation_signatures_match_calibre_9_12_adapter(self):
        methods = self.class_methods("calibre/db/cache.py", "Cache")
        expected_prefixes = {
            "pref": ["self", "name", "default", "namespace", "get_default_from_defaults"],
            "get_metadata": ["self", "book_id", "get_cover", "get_user_categories", "cover_as_data"],
            "cover": ["self", "book_id", "as_file", "as_image", "as_path", "as_pixmap"],
            "copy_format_to": ["self", "book_id", "fmt", "dest", "use_hardlink", "report_file_size"],
            "formats": ["self", "book_id", "verify_formats"],
            "list_extra_files": ["self", "book_id", "use_cache", "pattern"],
            "copy_extra_file_to": ["self", "book_id", "relpath", "stream_or_path"],
            "set_field": ["self", "name", "book_id_to_val_map", "allow_case_change", "do_path_update"],
            "set_metadata": ["self", "book_id", "mi", "ignore_errors", "force_changes"],
            "add_format": ["self", "book_id", "fmt", "stream_or_path", "replace", "run_hooks", "dbapi"],
            "has_format": ["self", "book_id", "fmt"],
            "remove_formats": ["self", "formats_map", "db_only"],
            "create_book_entry": ["self", "mi", "cover", "add_duplicates", "force_id", "apply_import_tags", "preserve_uuid"],
            "add_books": ["self", "books", "add_duplicates", "apply_import_tags", "preserve_uuid", "run_hooks", "dbapi"],
            "remove_books": ["self", "book_ids", "permanent"],
            "set_cover": ["self", "book_id_data_map"],
            "merge_book_metadata": ["self", "dest_id", "src_ids", "replace_cover", "save_alternate_cover"],
            "data_for_find_identical_books": ["self"],
        }
        for name, prefix in expected_prefixes.items():
            self.assertIn(name, methods)
            self.assertEqual(methods[name][: len(prefix)], prefix)

    def test_conversion_contracts_match_calibre_9_12_mapping(self):
        tool_functions = self.top_level_functions("calibre/gui2/tools.py")
        self.assertEqual(
            tool_functions["convert_single_ebook"][:6],
            ["parent", "db", "book_ids", "auto_conversion", "out_format", "show_no_format_warning"],
        )

        customise_functions = self.top_level_functions("calibre/customize/ui.py")
        self.assertEqual(customise_functions["run_plugins_on_postconvert"][:3], ["db", "book_id", "fmt"])

    def test_job_and_copy_contracts_match_calibre_9_12_mapping(self):
        job_methods = self.class_methods("calibre/gui2/jobs.py", "JobManager")
        self.assertEqual(job_methods["run_job"][:3], ["self", "done", "name"])
        self.assertEqual(job_methods["run_threaded_job"][:2], ["self", "job"])

        copy_functions = self.top_level_functions("calibre/db/copy_to_library.py")
        self.assertEqual(
            copy_functions["copy_one_book"][:8],
            ["book_id", "src_db", "dest_db", "duplicate_action", "automerge_action", "preserve_date", "identical_books_data", "preserve_uuid"],
        )
        self.assertEqual(
            copy_functions["automerge_book"][:7],
            ["automerge_action", "book_id", "mi", "identical_book_list", "newdb", "format_map", "extra_file_map"],
        )
        self.assertEqual(
            copy_functions["postprocess_copy"][:7],
            ["book_id", "new_book_id", "new_authors", "db", "newdb", "identical_books_data", "duplicate_action"],
        )

        copy_action_functions = self.top_level_functions("calibre/gui2/actions/copy_to_library.py")
        self.assertEqual(
            copy_action_functions["ask_about_cc_mismatch"][:5],
            ["gui", "db", "newdb", "missing_cols", "incompatible_cols"],
        )

        worker_methods = self.class_methods("calibre/gui2/actions/copy_to_library.py", "Worker")
        self.assertEqual(
            worker_methods["__init__"][:8],
            ["self", "ids", "db", "loc", "progress", "done", "delete_after", "add_duplicates"],
        )
        self.assertEqual(worker_methods["do_one"][:4], ["self", "num", "book_id", "newdb"])

    def test_save_to_disk_contracts_match_calibre_9_12_mapping(self):
        legacy_methods = self.class_methods("calibre/db/legacy.py", "LibraryDatabase")
        self.assertEqual(
            legacy_methods["get_metadata"][:6],
            ["self", "index", "index_is_id", "get_cover", "get_user_categories", "cover_as_data"],
        )
        self.assertEqual(legacy_methods["formats"][:4], ["self", "index", "index_is_id", "verify_formats"])

        library_save_functions = self.top_level_functions("calibre/library/save_to_disk.py")
        self.assertEqual(library_save_functions["save_to_disk"][:5], ["db", "ids", "root", "opts", "callback"])
        self.assertEqual(library_save_functions["sanitize_args"][:2], ["root", "opts"])

        save_functions = self.top_level_functions("calibre/gui2/save.py")
        self.assertEqual(save_functions["ensure_unique_components"][:1], ["data"])

        saver_methods = self.class_methods("calibre/gui2/save.py", "Saver")
        self.assertEqual(
            saver_methods["__init__"][:7],
            ["self", "book_ids", "db", "opts", "root", "parent", "pool"],
        )
        self.assertEqual(saver_methods["collect_data"][:2], ["self", "book_id"])
        self.assertEqual(saver_methods["write_book"][:4], ["self", "book_id", "mi", "components"])
        self.assertEqual(saver_methods["write_fmt"][:4], ["self", "book_id", "fmt", "base_path"])
        self.assertEqual(saver_methods["break_cycles"][:1], ["self"])

    def test_email_contracts_match_calibre_9_12_mapping(self):
        smtp_functions = self.top_level_functions("calibre/utils/smtp.py")
        self.assertEqual(smtp_functions["config"][:1], ["defaults"])

        email_functions = self.top_level_functions("calibre/gui2/email.py")
        self.assertEqual(
            email_functions["send_mails"][:8],
            ["jobnames", "callback", "attachments", "to_s", "subjects", "texts", "attachment_names", "job_manager"],
        )
        self.assertEqual(email_functions["email_news"][:5], ["mi", "remove", "get_fmts", "done", "job_manager"])

        sendmail_methods = self.class_methods("calibre/gui2/email.py", "Sendmail")
        self.assertEqual(
            sendmail_methods["__call__"][:9],
            ["self", "attachment", "aname", "to", "subject", "text", "log", "abort", "notifications"],
        )
        self.assertEqual(
            sendmail_methods["sendmail"][:7],
            ["self", "attachment", "aname", "to", "subject", "text", "log"],
        )

        email_mixin_methods = self.class_methods("calibre/gui2/email.py", "EmailMixin")
        self.assertEqual(
            email_mixin_methods["send_multiple_by_mail"][:3],
            ["self", "recipients", "delete_from_library"],
        )
        self.assertEqual(
            email_mixin_methods["send_by_mail"][:8],
            ["self", "to", "fmts", "delete_from_library", "subject", "send_ids", "do_auto_convert", "specific_format"],
        )
        self.assertEqual(email_mixin_methods["email_sent"][:3], ["self", "job", "remove"])

    def test_threaded_job_content_server_and_device_contracts_match_calibre_9_12_mapping(self):
        threaded_job_methods = self.class_methods("calibre/gui2/threaded_jobs.py", "ThreadedJob")
        self.assertEqual(
            threaded_job_methods["__init__"][:10],
            ["self", "type_", "description", "func", "args", "kwargs", "callback", "max_concurrent_count", "killable", "log"],
        )

        main_methods = self.class_methods("calibre/gui2/ui.py", "Main")
        self.assertEqual(main_methods["start_content_server"][:2], ["self", "check_started"])

        device_methods = self.class_methods("calibre/gui2/device.py", "DeviceManager")
        self.assertEqual(
            device_methods["create_job_step"][:7],
            ["self", "func", "done", "description", "to_job", "args", "kwargs"],
        )
        self.assertEqual(
            device_methods["prepare_addable_books"][:4],
            ["self", "done", "paths", "add_as_step_to_job"],
        )
        self.assertEqual(
            device_methods["sync_booklists"][:5],
            ["self", "done", "booklists", "plugboards", "add_as_step_to_job"],
        )
        self.assertEqual(
            device_methods["upload_books"][:9],
            ["self", "done", "files", "names", "on_card", "titles", "metadata", "plugboards", "add_as_step_to_job"],
        )
        self.assertEqual(
            device_methods["save_books"][:5],
            ["self", "done", "paths", "target", "add_as_step_to_job"],
        )


if __name__ == "__main__":
    unittest.main()
