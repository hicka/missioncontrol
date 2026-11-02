import time

import skills


def test_skill_lifecycle_and_sync():
    slug = skills.save_skill("tester", "CI Drill Skill", "for tests", "1. step one")
    assert any(s["slug"] == slug for s in skills.list_skills())
    assert "step one" in skills.read_skill("CI Drill Skill")

    exported = skills.export_skills(0)
    mine = next(e for e in exported if e["slug"] == slug)

    # merge an OLDER copy: must not overwrite
    older = {**mine, "mtime": mine["mtime"] - 100, "content": "stale"}
    assert skills.merge_skills([older]) == 0
    assert "step one" in skills.read_skill("CI Drill Skill")

    # merge a NEWER copy: wins
    newer = {**mine, "mtime": time.time() + 100, "content": "---\nname: CI Drill Skill\n---\n\n2. updated"}
    assert skills.merge_skills([newer]) == 1
    assert "updated" in skills.read_skill("CI Drill Skill")

    assert skills.delete_skill("CI Drill Skill")
