"""
Управление квотами YouTube Data API.

Отслеживание использованных units по Google Cloud проектам,
автоматическое переключение между проектами при исчерпании квоты.
"""

import logging
import json
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

QUOTA_STATE_FILE = Path(__file__).parent.parent / "logs" / "quota_state.json"
UPLOAD_COST = 1600  # units за один upload
DEFAULT_DAILY_QUOTA = 10000  # units/день/проект


class QuotaManager:
    """Менеджер квот YouTube API по Google Cloud проектам."""

    def __init__(self, account_groups: dict):
        """
        Args:
            account_groups: dict из config.yaml секция account_groups.
        """
        self.account_groups = account_groups
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """Загрузить состояние квот из файла."""
        if QUOTA_STATE_FILE.exists():
            with open(QUOTA_STATE_FILE, "r") as f:
                state = json.load(f)
            # Сброс если новый день (Pacific Time, полночь)
            if self._is_new_day(state.get("last_reset", "")):
                return self._fresh_state()
            return state
        return self._fresh_state()

    def _fresh_state(self) -> dict:
        """Создать свежее состояние (все квоты = 0 использовано)."""
        state = {
            "last_reset": datetime.now(timezone.utc).isoformat(),
            "projects": {}
        }
        for group_id, group in self.account_groups.items():
            state["projects"][group_id] = {
                "used_units": 0,
                "daily_quota": group.get("daily_quota", DEFAULT_DAILY_QUOTA)
            }
        return state

    def _is_new_day(self, last_reset: str) -> bool:
        """Проверить, нужно ли сбросить квоты (новый день по PT)."""
        if not last_reset:
            return True
        
        try:
            from datetime import datetime, timezone, timedelta
            import pytz
            
            # YouTube квоты сбрасываются в полночь Pacific Time
            pacific = pytz.timezone('America/Los_Angeles')
            
            last_reset_dt = datetime.fromisoformat(last_reset)
            now = datetime.now(timezone.utc)
            
            # Конвертировать в Pacific Time
            last_reset_pt = last_reset_dt.astimezone(pacific).date()
            now_pt = now.astimezone(pacific).date()
            
            return now_pt > last_reset_pt
            
        except Exception as e:
            logger.warning(f"Ошибка при проверке сброса квоты: {e}")
            # На всякий случай сбрасываем если прошло >25 часов
            try:
                last_reset_dt = datetime.fromisoformat(last_reset)
                now = datetime.now(timezone.utc)
                hours_passed = (now - last_reset_dt).total_seconds() / 3600
                return hours_passed > 25
            except Exception:
                return True

    def _save_state(self):
        """Сохранить состояние квот в файл."""
        QUOTA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(QUOTA_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def get_project_for_upload(self) -> str | None:
        """
        Получить ID проекта с достаточной квотой для одной загрузки.

        Returns:
            group_id проекта или None если все исчерпаны.
        """
        for group_id, project in self.state["projects"].items():
            remaining = project["daily_quota"] - project["used_units"]
            if remaining >= UPLOAD_COST:
                return group_id
        logger.warning("Все квоты исчерпаны. Загрузка отложена до следующих суток.")
        return None

    def record_upload(self, group_id: str):
        """Записать использование квоты после успешной загрузки."""
        self.state["projects"][group_id]["used_units"] += UPLOAD_COST
        self._save_state()
        remaining = (self.state["projects"][group_id]["daily_quota"]
                     - self.state["projects"][group_id]["used_units"])
        logger.info(f"Квота {group_id}: использовано +{UPLOAD_COST}, "
                    f"осталось {remaining} units")

    def get_status(self) -> dict:
        """Получить текущее состояние всех квот."""
        status = {}
        for group_id, project in self.state["projects"].items():
            status[group_id] = {
                "used": project["used_units"],
                "quota": project["daily_quota"],
                "remaining": project["daily_quota"] - project["used_units"],
                "uploads_left": (project["daily_quota"] - project["used_units"])
                                // UPLOAD_COST
            }
        return status
