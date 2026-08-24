from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from social_ops_agent.desktop import MainWindow


def test_desktop_has_conversation_and_confirmation_controls(tmp_path: Path) -> None:
    registry = tmp_path / "sessions.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": [
                    {
                        "session_ref": "sess_douyin_abcdefghijklmnopqrstuvwx",
                        "platform": "douyin",
                        "provider": "bitbrowser",
                        "profile_id": "profile-1",
                        "profile_name": "抖音账号 01",
                        "api_url": "http://127.0.0.1:54345",
                        "created_at": "2026-08-23T00:00:00+00:00",
                        "updated_at": "2026-08-23T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow(registry_path=registry, output_root=tmp_path / "downloads")

    assert "社媒任务助手" in window.windowTitle()
    assert window.session_combo.itemText(0).startswith("抖音")
    assert window.plan_button.text().startswith("生成计划")
    assert window.execute_button.text() == "确认并执行计划"
    assert not window.execute_button.isEnabled()
    window.close()
    app.processEvents()
