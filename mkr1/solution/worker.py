import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel
from PySide6.QtCore import QThread, Signal

from parsers import ImageAsset, extract_text_from_file, load_submission_target
from schemas import BatchAuditResponse, ExtractedRequirement, GuidelinesPlan, RequirementCheck

T = TypeVar("T", bound=BaseModel)

DEFAULT_EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"


class AuditCancelledError(Exception):
    pass


def chunk_list(items: list[ExtractedRequirement], batch_size: int) -> list[list[ExtractedRequirement]]:
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")
    return cleaned[:80] if cleaned else "Без_предмета"


class AuditWorker(QThread):
    log_signal = Signal(str)
    progress_signal = Signal(int, int, str)
    batch_ready_signal = Signal(list)
    ask_retry_signal = Signal(str, str)
    finished_signal = Signal(dict)
    cancelled_signal = Signal(str, list)
    error_signal = Signal(str)

    def __init__(
        self,
        api_key: str,
        subject_name: str,
        guidelines_path: Path,
        report_path: Path,
        variant: str,
        batch_size: int = 5,
        model_name: str = "gemini-3.5-flash-lite",
        evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    ):
        super().__init__()
        self.api_key = api_key
        self.subject_folder = sanitize_folder_name(subject_name)
        self.guidelines_path = guidelines_path
        self.report_path = report_path
        self.variant = variant
        self.batch_size = max(1, batch_size)
        self.model_name = model_name
        self.evidence_dir = evidence_dir

        self._client: genai.Client | None = None
        self._decision_event = threading.Event()
        self._should_continue = False
        self._abort_requested = False

    def resolve_retry_dialog(self, continue_retries: bool) -> None:
        self._should_continue = continue_retries
        self._decision_event.set()

    def request_abort(self) -> None:
        self._abort_requested = True
        self._should_continue = False
        self._decision_event.set()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def _classify_retryable_error(self, exc: Exception) -> tuple[bool, int, str]:
        code = getattr(exc, "code", None) if isinstance(exc, errors.APIError) else None
        msg = str(exc).lower()

        if code == 503 or "503" in msg or "unavailable" in msg or "overloaded" in msg:
            return True, 5, "503 Service Unavailable"

        if code == 429 or "429" in msg or "resource_exhausted" in msg or "quota" in msg or "rate limit" in msg:
            return True, 15, "429 Too Many Requests / Quota"

        return False, 0, ""

    def _check_abort(self) -> None:
        if self._abort_requested:
            raise AuditCancelledError("Перевірку зупинено користувачем.")

    def _call_with_retry(self, fn: Callable[[], T], step_label: str) -> T:
        while True:
            last_label = "API Error"
            base_delay = 5

            for attempt in range(1, 6):
                self._check_abort()
                try:
                    return fn()
                except Exception as exc:
                    self._check_abort()
                    retryable, base_delay, err_label = self._classify_retryable_error(exc)
                    if not retryable:
                        raise

                    last_label = err_label
                    wait_sec = attempt * base_delay
                    self.log_signal.emit(
                        f"[{err_label}] {step_label}: спроба {attempt}/5 не вдалася. "
                        f"Повтор через {wait_sec} с..."
                    )
                    for remaining in range(wait_sec, 0, -1):
                        self._check_abort()
                        self.progress_signal.emit(
                            -1, -1, f"Помилка {err_label} ({step_label}). Повтор через {remaining} с (спроба {attempt}/5)..."
                        )
                        time.sleep(1)

            self._decision_event.clear()
            self.log_signal.emit(
                f"[УВАГА] {step_label}: 5 спроб поспіль завершилися помилкою ({last_label}). Очікування рішення користувача..."
            )
            self.ask_retry_signal.emit(
                f"Помилка сервера ({last_label}) — Вичерпано 5 спроб",
                f"Сервер Gemini API повернув помилку «{last_label}» 5 разів поспіль "
                f"на етапі «{step_label}».\n\n"
                f"Бажаєте продовжити (запустити новий цикл із 5 спроб) чи закрити перевірку?",
            )
            self._decision_event.wait()

            if not self._should_continue or self._abort_requested:
                raise AuditCancelledError(f"Перевірку закрито користувачем після 5 невдалих спроб ({last_label}).")

            self.log_signal.emit(f"[ПРОДОВЖЕННЯ] Перезапуск циклу з 5 спроб для «{step_label}»...")

    def _build_batch_contents(
        self,
        reqs_formatted: str,
        report_text: str,
        images: list[ImageAsset],
    ) -> list:
        contents: list = [
            f"Дисципліна: {self.subject_folder}\n"
            f"Варіант: {self.variant}\n\n"
            f"Перевір ТІЛЬКИ наступний батч вимог за текстовими та візуальними матеріалами студента. "
            f"Уважно аналізуй прикріплені зображення (скріншоти термінала, діаграми, схеми мереж, сторінки PDF) "
            f"для перевірки графічних вимог. Став WARNING лише якщо зображення справді відсутнє або нечитабельне.\n\n"
            f"ВИМОГИ БАТЧУ:\n{reqs_formatted}\n\n"
            f"ТЕКСТОВІ МАТЕРІАЛИ СТУДЕНТА:\n{report_text}"
        ]

        if images:
            contents.append("\nПРИКРІПЛЕНІ ЗОБРАЖЕННЯ / СТОРІНКИ ЗВІТУ:")
            for img in images:
                contents.append(f"Зображення: {img.name}")
                contents.append(types.Part.from_bytes(data=img.data, mime_type=img.mime_type))

        return contents

    def run(self) -> None:
        start_total = time.perf_counter()
        all_checks: list[RequirementCheck] = []
        loaded_files: list[str] = []
        image_names: list[str] = []
        total_prompt_tokens = 0
        total_output_tokens = 0

        try:
            self._check_abort()
            self.progress_signal.emit(0, 100, "Читання методички та матеріалів роботи...")
            self.log_signal.emit(f"Папка предмета: evidence/{self.subject_folder}/")
            self.log_signal.emit(f"Читання методички: {self.guidelines_path.name}")
            guidelines_text = extract_text_from_file(self.guidelines_path)

            if not guidelines_text.strip():
                raise ValueError("Файл методички порожній або не вдалося витягти текст (можливо, це скан без OCR).")

            report_text, loaded_files, images = load_submission_target(self.report_path)
            image_names = [img.name for img in images]
            self.log_signal.emit(
                f"Зчитано файлів ({len(loaded_files)}): {', '.join(loaded_files)}"
            )
            if images:
                self.log_signal.emit(
                    f"Витягнуто зображень/сторінок для візуального аналізу ({len(images)}): {', '.join(image_names)}"
                )

            self._client = genai.Client(api_key=self.api_key)

            self._check_abort()
            self.progress_signal.emit(5, 100, "Етап 1: Аналіз методички та виділення вимог...")
            self.log_signal.emit("Виділення списку вимог з методички...")

            def extract_plan_call() -> tuple[GuidelinesPlan, int, int]:
                assert self._client is not None
                resp = self._client.models.generate_content(
                    model=self.model_name,
                    contents=(
                        f"ВАРІАНТ СТУДЕНТА: {self.variant}\n\n"
                        f"Витягни з тексту методички всі загальні вимоги до оформлення/змісту лабораторної "
                        f"та всі конкретні вимоги для вказаного варіанта.\n\n"
                        f"МЕТОДИЧКА:\n{guidelines_text}"
                    ),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=GuidelinesPlan,
                        temperature=0.1,
                    ),
                )
                usage = resp.usage_metadata
                return (
                    GuidelinesPlan.model_validate_json(resp.text),
                    getattr(usage, "prompt_token_count", 0),
                    getattr(usage, "candidates_token_count", 0),
                )

            plan, p_tok, o_tok = self._call_with_retry(extract_plan_call, "Аналіз методички")
            total_prompt_tokens += p_tok
            total_output_tokens += o_tok

            if not plan.requirements:
                raise ValueError(
                    "Модель не змогла виділити жодної вимоги з методички. "
                    "Перевірте, чи правильно обрано файл методички та варіант."
                )

            batches = chunk_list(plan.requirements, self.batch_size)
            total_batches = len(batches)
            self.log_signal.emit(
                f"Знайдено вимог: {len(plan.requirements)}. Розбито на {total_batches} батч(ів) по {self.batch_size} вимоги."
            )

            for idx, batch in enumerate(batches, start=1):
                self._check_abort()
                pct = int(10 + (idx - 1) / max(1, total_batches) * 85)
                self.progress_signal.emit(pct, 100, f"Перевірка батчу {idx}/{total_batches}...")
                self.log_signal.emit(f"Запуск батчу {idx}/{total_batches} ({len(batch)} вимог)...")

                reqs_formatted = "\n".join(f"- [{r.requirement_id}] {r.requirement_text}" for r in batch)
                batch_contents = self._build_batch_contents(reqs_formatted, report_text, images)

                def check_batch_call() -> tuple[BatchAuditResponse, int, int]:
                    assert self._client is not None
                    resp = self._client.models.generate_content(
                        model=self.model_name,
                        contents=batch_contents,
                        config=types.GenerateContentConfig(
                            system_instruction=(
                                "Ти суворий, але об'єктивний викладач університету. "
                                "Для кожної вимоги вкажи точний доказ зі звіту, коду або прикріплених зображень "
                                "(або 'Відсутньо') та чітку інструкцію виправлення."
                            ),
                            response_mime_type="application/json",
                            response_schema=BatchAuditResponse,
                            temperature=0.1,
                        ),
                    )
                    usage = resp.usage_metadata
                    return (
                        BatchAuditResponse.model_validate_json(resp.text),
                        getattr(usage, "prompt_token_count", 0),
                        getattr(usage, "candidates_token_count", 0),
                    )

                batch_res, bp_tok, bo_tok = self._call_with_retry(
                    check_batch_call, f"Батч {idx}/{total_batches}"
                )
                total_prompt_tokens += bp_tok
                total_output_tokens += bo_tok

                all_checks.extend(batch_res.checks)
                self.batch_ready_signal.emit(batch_res.checks)
                self.log_signal.emit(f"Батч {idx}/{total_batches} успішно завершено.")

            self._check_abort()

            payload = self.save_report(
                all_checks=all_checks,
                loaded_files=loaded_files,
                image_names=image_names,
                elapsed=round(time.perf_counter() - start_total, 2),
                prompt_tokens=total_prompt_tokens,
                output_tokens=total_output_tokens,
            )
            self.progress_signal.emit(100, 100, f"Завершено -> evidence/{self.subject_folder}/{Path(payload['saved_to']).name}")
            self.finished_signal.emit(payload)

        except AuditCancelledError as cancel_err:
            self.log_signal.emit(f"[ЗУПИНЕНО] {cancel_err}")
            self.progress_signal.emit(0, 100, "Зупинено")
            self.cancelled_signal.emit(
                str(cancel_err),
                [
                    {
                        "checks": [c.model_dump() for c in all_checks],
                        "loaded_files": loaded_files,
                        "image_names": image_names,
                        "elapsed": round(time.perf_counter() - start_total, 2),
                        "prompt_tokens": total_prompt_tokens,
                        "output_tokens": total_output_tokens,
                    }
                ],
            )
        except Exception as exc:
            if self._abort_requested:
                self.log_signal.emit("[ЗУПИНЕНО] Перевірку зупинено користувачем.")
                self.progress_signal.emit(0, 100, "Зупинено")
                self.cancelled_signal.emit(
                    "Перевірку зупинено користувачем.",
                    [
                        {
                            "checks": [c.model_dump() for c in all_checks],
                            "loaded_files": loaded_files,
                            "image_names": image_names,
                            "elapsed": round(time.perf_counter() - start_total, 2),
                            "prompt_tokens": total_prompt_tokens,
                            "output_tokens": total_output_tokens,
                        }
                    ],
                )
            else:
                self.error_signal.emit(f"Критична помилка: {exc}")
        finally:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def save_report(
        self,
        all_checks: list[RequirementCheck],
        loaded_files: list[str],
        image_names: list[str],
        elapsed: float,
        prompt_tokens: int,
        output_tokens: int,
    ) -> dict:
        total = len(all_checks)
        passed = sum(1 for c in all_checks if c.status == "PASS")
        warnings = sum(1 for c in all_checks if c.status == "WARNING")
        failed = sum(1 for c in all_checks if c.status == "FAIL")
        score = int(round(((passed + 0.5 * warnings) / total) * 100)) if total > 0 else 0

        target_dir = self.evidence_dir / self.subject_folder
        target_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        readable_date = now.strftime("%Y-%m-%d %H:%M:%S")
        log_file = target_dir / f"audit_{timestamp}.json"

        payload = {
            "timestamp": timestamp,
            "date_human": readable_date,
            "subject": self.subject_folder,
            "guidelines_file": self.guidelines_path.name,
            "report_target": self.report_path.name,
            "variant": self.variant,
            "files_checked": loaded_files,
            "images_checked": image_names,
            "batch_size": self.batch_size,
            "readiness_score": score,
            "summary_counts": {
                "total": total,
                "pass": passed,
                "warning": warnings,
                "fail": failed,
            },
            "metrics": {
                "model": self.model_name,
                "elapsed_seconds": elapsed,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "total_tokens": prompt_tokens + output_tokens,
            },
            "checks": [c.model_dump() for c in all_checks],
            "saved_to": str(log_file),
        }

        log_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        history_file = self.evidence_dir / "history.jsonl"
        history_entry = {
            "timestamp": timestamp,
            "date_human": readable_date,
            "subject": self.subject_folder,
            "relative_path": f"{self.subject_folder}/{log_file.name}",
            "report_target": self.report_path.name,
            "variant": self.variant,
            "readiness_score": score,
            "elapsed_seconds": elapsed,
            "total_tokens": prompt_tokens + output_tokens,
            "model": self.model_name,
        }
        with history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")

        return payload