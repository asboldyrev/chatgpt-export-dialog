import json
import tempfile
import unittest
from pathlib import Path

from export_dialogs import get_project_id, load_project_names, project_folder_names, main


class ProjectsTests(unittest.TestCase):
    def test_mapping_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "projects.json"
            p.write_text(json.dumps({"g-p-one": "Hermes Agent"}), encoding="utf-8")
            names = load_project_names(p)
            self.assertEqual(project_folder_names(["g-p-one", "g-p-two"], names),
                             {"g-p-one": "Hermes_Agent", "g-p-two": "g-p-two"})

    def test_conflicting_names_stay_separate(self):
        result = project_folder_names(["a", "b"], {"a": "Same Name", "b": "Same/Name"})
        self.assertEqual(len(set(result.values())), 2)

    def test_invalid_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            path.write_text('{"id": ""}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_project_names(path)

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "conversations.json"
            def conv(cid, title, project=None):
                data = {"id": cid, "title": title, "create_time": 1,
                        "messages": [{"author": {"role": "user"}, "content": {"parts": ["Hello"]}}]}
                if project:
                    data["gizmo_id"] = project
                return data
            source.write_text(json.dumps([conv("1", "Task", "g-p-one"), conv("2", "General")]), encoding="utf-8")
            mapping = root / "projects.json"
            mapping.write_text('{"g-p-one": "Hermes Agent"}', encoding="utf-8")
            out = root / "result"
            self.assertEqual(main([str(source), "-o", str(out), "--projects", str(mapping)]), 0)
            self.assertTrue((out / "projects" / "Hermes_Agent" / "001_Task" / "dialogue.md").exists())
            self.assertTrue((out / "chats" / "001_General" / "dialogue.md").exists())
            self.assertIn("Hermes Agent", (out / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
