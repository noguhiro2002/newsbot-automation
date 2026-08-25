from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, DiscordSettings, load_discord_settings
from .db import EDITORIAL_REASON_CODES, NewsbotStore, validate_editorial_reason
from .discord_client import parse_message_interval
from .feedback import INTERESTED_EMOJI, record_interested_feedback
from .missed_item import (
    format_missed_item_result,
    record_researched_missed_item,
    research_missed_item_with_codex,
)
from .render import (
    render_published_message,
    render_review_message,
    render_weekly_detail_message,
    render_weekly_digest_overview,
    order_weekly_drafts,
)
from .review import build_feedback_custom_id, build_review_custom_id
from .source_check import SourceCheckResult, check_source_with_codex

try:
    import discord
except ImportError as exc:  # pragma: no cover - guarded at runtime.
    discord = None
    DISCORD_IMPORT_ERROR = exc
else:  # pragma: no cover
    DISCORD_IMPORT_ERROR = None


WEEKLY_PUBLISH_TZ = ZoneInfo("Asia/Tokyo")
WEEKLY_PUBLISH_DAY = 0
WEEKLY_PUBLISH_TIME = time(hour=8, minute=0)
DEFAULT_MANUAL_REVIEW_LOOKBACK_DAYS = 7
DEFAULT_REVIEW_TOPIC = "lab_automation"
DEFAULT_REVIEW_CADENCE = "weekly"
STATUS_UPDATE_INTERVAL_SECONDS = 5
STATUS_LOG_LINE_LIMIT = 10


def next_weekly_publish_at(now: datetime | None = None) -> datetime:
    current = now.astimezone(WEEKLY_PUBLISH_TZ) if now else datetime.now(WEEKLY_PUBLISH_TZ)
    days_ahead = (WEEKLY_PUBLISH_DAY - current.weekday()) % 7
    candidate = datetime.combine(
        current.date() + timedelta(days=days_ahead),
        WEEKLY_PUBLISH_TIME,
        tzinfo=WEEKLY_PUBLISH_TZ,
    )
    if candidate <= current:
        candidate += timedelta(days=7)
    return candidate


def format_schedule(dt: datetime) -> str:
    return dt.astimezone(WEEKLY_PUBLISH_TZ).strftime("%Y-%m-%d %H:%M JST")


def _manual_review_default_lookback_days() -> int:
    raw = (
        os.getenv("NEWSBOT_MANUAL_REVIEW_LOOKBACK_DAYS", "").strip()
        or os.getenv("NEWSBOT_LOOKBACK_DAYS", "").strip()
    )
    if not raw:
        return DEFAULT_MANUAL_REVIEW_LOOKBACK_DAYS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MANUAL_REVIEW_LOOKBACK_DAYS
    return value if value > 0 else DEFAULT_MANUAL_REVIEW_LOOKBACK_DAYS


def default_review_topic() -> str:
    return os.getenv("NEWSBOT_REVIEW_TOPIC", "").strip() or os.getenv(
        "NEWSBOT_GENERATE_REVIEW_TOPIC", ""
    ).strip() or DEFAULT_REVIEW_TOPIC


def default_review_cadence() -> str:
    return os.getenv("NEWSBOT_REVIEW_CADENCE", "").strip() or os.getenv(
        "NEWSBOT_GENERATE_REVIEW_CADENCE", ""
    ).strip() or DEFAULT_REVIEW_CADENCE


@dataclass(frozen=True)
class ReviewCollectionRequest:
    topic: str = DEFAULT_REVIEW_TOPIC
    cadence: str = DEFAULT_REVIEW_CADENCE
    lookback_days: int = DEFAULT_MANUAL_REVIEW_LOOKBACK_DAYS
    period: str = ""

    @classmethod
    def default(cls) -> "ReviewCollectionRequest":
        return cls(
            topic=default_review_topic(),
            cadence=default_review_cadence(),
            lookback_days=_manual_review_default_lookback_days(),
        )


def resolve_review_collection_period(
    request: ReviewCollectionRequest,
    now: datetime | None = None,
) -> str:
    if request.period.strip():
        return request.period.strip()
    current = (now or datetime.now(WEEKLY_PUBLISH_TZ)).astimezone(WEEKLY_PUBLISH_TZ)
    start = current - timedelta(days=request.lookback_days)
    return f"{start:%Y-%m-%d} to {current:%Y-%m-%d} (JST)"


def build_review_collection_command(
    request: ReviewCollectionRequest,
    *,
    python_bin: str | None = None,
    codex_bin: str | None = None,
    db_path: str | Path | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> list[str]:
    command = [
        python_bin or os.getenv("NEWSBOT_PYTHON_BIN", "").strip() or sys.executable,
        "scripts/generate_payload_openai.py",
        "--topic",
        request.topic,
        "--cadence",
        request.cadence,
        "--submit-review",
        "--config",
        str(config_path),
    ]
    if db_path is not None:
        command.extend(["--db", str(db_path)])
    if request.period.strip():
        command.extend(["--period", request.period.strip()])
    else:
        command.extend(["--lookback-days", str(request.lookback_days)])
    resolved_codex_bin = codex_bin if codex_bin is not None else os.getenv("NEWSBOT_CODEX_BIN", "").strip()
    if resolved_codex_bin:
        command.extend(["--codex-bin", resolved_codex_bin])
    return command


def format_review_collection_confirmation(request: ReviewCollectionRequest) -> str:
    period = resolve_review_collection_period(request)
    lines = [
        "Manual review candidate collection",
        "",
        f"Topic: `{request.topic}`",
        f"Cadence: `{request.cadence}`",
        f"Search period: `{period}`",
        f"Lookback days: `{request.lookback_days}`" if not request.period.strip() else "Lookback days: ignored",
        "",
        "Press Start to run candidate collection and submit review drafts to Discord.",
    ]
    return "\n".join(lines)


def summarize_review_collection_output(lines: list[str]) -> str:
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[newsbot-generate] "):
            return stripped.removeprefix("[newsbot-generate] ")
        if stripped.startswith("Submitted review drafts:"):
            return stripped
        if stripped.startswith("Payload OK:"):
            return stripped
    return "Waiting for output..."


def format_review_collection_status(
    request: ReviewCollectionRequest,
    *,
    status: str,
    started_at: datetime,
    command: list[str],
    output_lines: list[str],
    returncode: int | None = None,
) -> str:
    period = resolve_review_collection_period(request, started_at)
    elapsed = datetime.now(WEEKLY_PUBLISH_TZ) - started_at
    elapsed_seconds = max(0, int(elapsed.total_seconds()))
    headline = f"Review candidate collection: {status}"
    if returncode is not None:
        headline += f" (exit={returncode})"
    recent_lines = output_lines[-STATUS_LOG_LINE_LIMIT:]
    recent = "\n".join(line[-180:] for line in recent_lines) or "(no output yet)"
    content = (
        f"{headline}\n"
        f"Topic: `{request.topic}` / `{request.cadence}`\n"
        f"Search period: `{period}`\n"
        f"Elapsed: `{elapsed_seconds}s`\n"
        f"Current step: `{summarize_review_collection_output(output_lines)}`\n"
        "\n"
        "Recent output:\n"
        f"```text\n{recent}\n```"
    )
    if len(content) <= 1900:
        return content
    compact_recent = "\n".join(line[-140:] for line in recent_lines[-5:]) or "(no output yet)"
    return (
        f"{headline}\n"
        f"Topic: `{request.topic}` / `{request.cadence}`\n"
        f"Search period: `{period}`\n"
        f"Elapsed: `{elapsed_seconds}s`\n"
        f"Current step: `{summarize_review_collection_output(output_lines)}`\n\n"
        f"Recent output:\n```text\n{compact_recent}\n```"
    )


def _parse_meta(value: str) -> tuple[str | None, list[str]]:
    raw = value.strip()
    if not raw:
        return None, []
    if "|" in raw:
        category_part, tags_part = raw.split("|", 1)
    else:
        category_part, tags_part = raw, ""
    category = category_part.strip() or None
    tags = [part.strip() for part in tags_part.split(",") if part.strip()]
    return category, tags


@dataclass(frozen=True)
class DraftModalDefaults:
    id: str
    title: str = ""
    body: str = ""
    category: str = ""
    source_url: str = ""
    tags: tuple[str, ...] = ()


def parse_review_message_for_modal(article_id: str, content: str | None) -> DraftModalDefaults:
    lines = (content or "").splitlines()
    category = ""
    source_url = ""
    title_lines: list[str] = []
    body_lines: list[str] = []
    section: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip()
        if line.startswith("Category:"):
            category = line.split(":", 1)[1].strip()
            continue
        if line == "Title:":
            section = "title"
            continue
        if line == "Body:":
            section = "body"
            continue
        if line.startswith("Source:"):
            source_url = line.split(":", 1)[1].strip()
            section = None
            continue
        if section == "title":
            title_lines.append(line)
        elif section == "body":
            body_lines.append(line)

    return DraftModalDefaults(
        id=article_id,
        title="\n".join(title_lines).strip(),
        body="\n".join(body_lines).strip(),
        category=category,
        source_url=source_url,
    )


if discord is not None:
    class ReviewActionsView(discord.ui.View):
        def __init__(
            self,
            article_id: str,
            *,
            include_cancel: bool = False,
            exclude: set[str] | None = None,
        ):
            super().__init__(timeout=None)
            excluded = exclude or set()
            actions: list[tuple[str, str, discord.ButtonStyle]] = []
            if include_cancel:
                actions.append(("cancel_weekly", "Cancel Weekly", discord.ButtonStyle.danger))
            elif "weekly" not in excluded:
                actions.append(("weekly", "Approve Weekly", discord.ButtonStyle.success))
            actions.extend(
                [
                    ("edit", "Edit Draft", discord.ButtonStyle.secondary),
                    ("check_source", "Check Source", discord.ButtonStyle.secondary),
                    ("breaking", "Publish Breaking", discord.ButtonStyle.danger),
                    ("hold", "Hold", discord.ButtonStyle.secondary),
                    ("reject", "Reject", discord.ButtonStyle.danger),
                ]
            )
            for index, (action, label, style) in enumerate(actions):
                if action in excluded:
                    continue
                self.add_item(
                    discord.ui.Button(
                        label=label,
                        style=style,
                        custom_id=build_review_custom_id(action, article_id),
                        row=0 if index < 5 else 1,
                    )
                )

    class FinalWeeklyView(discord.ui.View):
        def __init__(self, article_id: str):
            super().__init__(timeout=None)
            actions: list[tuple[str, str, discord.ButtonStyle]] = [
                ("ready_go", "Publish", discord.ButtonStyle.success),
                ("edit", "Edit Draft", discord.ButtonStyle.secondary),
                ("cancel_publish", "Cancel", discord.ButtonStyle.danger),
            ]
            for index, (action, label, style) in enumerate(actions):
                self.add_item(
                    discord.ui.Button(
                        label=label,
                        style=style,
                        custom_id=build_review_custom_id(action, article_id),
                        row=0 if index < 5 else 1,
                    )
                )

    class FinalSelectedView(discord.ui.View):
        def __init__(self, article_id: str):
            super().__init__(timeout=None)
            actions: list[tuple[str, str, discord.ButtonStyle]] = [
                ("edit", "Edit Draft", discord.ButtonStyle.secondary),
                ("cancel_publish", "Cancel", discord.ButtonStyle.danger),
            ]
            for index, (action, label, style) in enumerate(actions):
                self.add_item(
                    discord.ui.Button(
                        label=label,
                        style=style,
                        custom_id=build_review_custom_id(action, article_id),
                        row=0 if index < 5 else 1,
                    )
                )

    class DigestPublishView(discord.ui.View):
        def __init__(self, article_id: str):
            super().__init__(timeout=None)
            self.add_item(
                discord.ui.Button(
                    label="Reorder",
                    style=discord.ButtonStyle.primary,
                    custom_id=build_review_custom_id("reorder_digest", article_id),
                )
            )
            self.add_item(
                discord.ui.Button(
                    label="Publish Digest",
                    style=discord.ButtonStyle.success,
                    custom_id=build_review_custom_id("publish_digest", article_id),
                )
            )

    class ReorderDigestModal(discord.ui.Modal, title="Reorder Weekly Digest"):
        def __init__(self, bot: "NewsbotDiscordBot", topic: str, cadence: str, scheduled_at: str, message):
            super().__init__(timeout=300)
            self.bot = bot
            self.topic = topic
            self.cadence = cadence
            self.scheduled_at = scheduled_at
            self.message = message
            self.order_input = discord.ui.TextInput(
                label="New order, e.g. 3,1,2,4",
                placeholder="Use each current item number once.",
                max_length=200,
            )
            self.add_item(self.order_input)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not self.bot.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return
            if not self.bot.is_reviewer(interaction.user.id):
                await interaction.response.send_message("You are not allowed to operate reviewer controls.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            drafts = await self.bot.db_call(
                self.bot.store.list_final_ready_weekly_batch,
                self.topic,
                self.cadence,
                self.scheduled_at,
            )
            if not drafts:
                await interaction.followup.send("No final digest items are available to reorder.", ephemeral=True)
                return
            try:
                order_numbers = [
                    int(part.strip())
                    for part in self.order_input.value.replace("\n", ",").split(",")
                    if part.strip()
                ]
            except ValueError:
                await interaction.followup.send("Order must contain numbers like 3,1,2,4.", ephemeral=True)
                return
            expected = list(range(1, len(drafts) + 1))
            if sorted(order_numbers) != expected:
                await interaction.followup.send(
                    f"Order must use each number exactly once: {', '.join(str(value) for value in expected)}",
                    ephemeral=True,
                )
                return
            ordered_ids = [drafts[number - 1].id for number in order_numbers]
            updated = await self.bot.db_call(
                self.bot.store.reorder_final_ready_weekly,
                self.topic,
                self.cadence,
                ordered_ids,
                str(interaction.user.id),
                self.scheduled_at,
            )
            content = (
                "Final digest is ready.\n"
                "Review the order below. Use Reorder if needed, then click Publish Digest.\n\n"
                + render_weekly_digest_overview(updated)
            )
            if self.message:
                await self.message.edit(content=content, view=DigestPublishView(updated[0].id), suppress=True)
            await interaction.followup.send("Digest order updated.", ephemeral=True)

    class FeedbackView(discord.ui.View):
        def __init__(self, article_id: str, interested_count: int = 0):
            super().__init__(timeout=None)
            self.add_item(
                discord.ui.Button(
                    label=f"Interested ({interested_count})",
                    emoji=INTERESTED_EMOJI,
                    style=discord.ButtonStyle.primary,
                    custom_id=build_feedback_custom_id("interested", article_id),
                )
            )

    class EditorialDecisionModal(discord.ui.Modal, title="Editorial reason required"):
        def __init__(self, bot: "NewsbotDiscordBot", article_id: str, action: str):
            super().__init__(timeout=300)
            self.bot = bot
            self.article_id = article_id
            self.action = action
            self.reason_code = discord.ui.Select(
                placeholder="Select a reason code",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(label=reason_code, value=reason_code)
                    for reason_code in sorted(EDITORIAL_REASON_CODES)
                ],
            )
            self.note = discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=500,
            )
            self.add_item(discord.ui.Label(text="Reason code", component=self.reason_code))
            self.add_item(discord.ui.Label(text="Note (required for other)", component=self.note))

        async def on_submit(self, interaction: discord.Interaction) -> None:
            reason_code = self.reason_code.values[0].strip()
            try:
                validate_editorial_reason(reason_code, str(self.note.value).strip())
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            handler = getattr(self.bot, f"_handle_{self.action}")
            await handler(
                interaction,
                self.article_id,
                reason_code=reason_code,
                reason_note=str(self.note.value).strip(),
            )

    class BreakingConfirmView(discord.ui.View):
        def __init__(self, bot: "NewsbotDiscordBot", article_id: str, reason_code: str, reason_note: str):
            super().__init__(timeout=300)
            self.bot = bot
            self.article_id = article_id
            self.reason_code = reason_code
            self.reason_note = reason_note

        @discord.ui.button(label="Confirm Breaking Publish", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            if not self.bot.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return
            if not self.bot.is_reviewer(interaction.user.id):
                await interaction.response.send_message("You are not allowed to operate reviewer controls.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)

            draft = await self.bot.db_call(self.bot.store.get_draft, self.article_id)
            if draft is None:
                await interaction.edit_original_response(content="Draft not found.", view=None)
                return
            if draft.status == "published":
                await interaction.edit_original_response(
                    content="This draft can no longer be published as breaking.",
                    view=None,
                )
                return

            await self.bot.db_call(
                self.bot.store.record_review_action,
                self.article_id,
                str(interaction.user.id),
                "breaking_confirm",
                draft.to_dict(),
            )

            channel = self.bot.get_channel(int(self.bot.settings.publish_channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(self.bot.settings.publish_channel_id))
            published = await self.bot.send_channel_message(
                channel,
                render_published_message(draft, "breaking"),
                view=FeedbackView(self.article_id, 0),
                suppress_embeds=True,
            )
            await self.bot.db_call(
                self.bot.store.mark_published,
                self.article_id,
                str(channel.id),
                str(published.id),
                "breaking",
            )
            latest = await self.bot.db_call(self.bot.store.get_draft, self.article_id)
            await self.bot._record_editorial(self.article_id, str(interaction.user.id), "accepted", self.reason_code, self.reason_note)
            if latest is not None:
                await self.bot.db_call(
                    self.bot.store.record_review_action,
                    self.article_id,
                    str(interaction.user.id),
                    "publish_breaking",
                    None,
                    latest.to_dict(),
                )
            await self.bot.db_call(
                self.bot.store.record_delivery_event,
                article_id=self.article_id,
                topic=draft.topic,
                cadence=draft.cadence,
                publish_type="breaking",
                destination="discord",
                status="sent",
                discord_channel_id=str(channel.id),
                discord_message_id=str(published.id),
                message_text=render_published_message(draft, "breaking"),
            )
            self.bot.schedule_refresh_review_message(self.article_id)
            await interaction.edit_original_response(content="Breaking news published.", view=None)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            await interaction.response.edit_message(content="Breaking publish cancelled.", view=None)

    class ReviewCollectionModal(discord.ui.Modal, title="Manual Review Collection"):
        def __init__(self, bot: "NewsbotDiscordBot", request: ReviewCollectionRequest, message):
            super().__init__(timeout=300)
            self.bot = bot
            self.message = message
            self.topic_input = discord.ui.TextInput(
                label="Topic",
                default=request.topic[:100],
                max_length=100,
            )
            self.cadence_input = discord.ui.TextInput(
                label="Cadence",
                default=request.cadence[:40],
                max_length=40,
            )
            self.lookback_input = discord.ui.TextInput(
                label="Lookback days",
                default=str(request.lookback_days),
                max_length=4,
            )
            self.period_input = discord.ui.TextInput(
                label="Explicit period override",
                default=request.period[:200],
                required=False,
                max_length=200,
                placeholder="Example: 2026-06-29 to 2026-07-06 (JST)",
            )
            self.add_item(self.topic_input)
            self.add_item(self.cadence_input)
            self.add_item(self.lookback_input)
            self.add_item(self.period_input)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not self.bot.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return
            if not self.bot.is_reviewer(interaction.user.id):
                await interaction.response.send_message("You are not allowed to operate reviewer controls.", ephemeral=True)
                return
            try:
                lookback_days = int(self.lookback_input.value.strip())
            except ValueError:
                await interaction.response.send_message("Lookback days must be a positive integer.", ephemeral=True)
                return
            if lookback_days <= 0:
                await interaction.response.send_message("Lookback days must be a positive integer.", ephemeral=True)
                return

            request = ReviewCollectionRequest(
                topic=self.topic_input.value.strip() or default_review_topic(),
                cadence=self.cadence_input.value.strip() or default_review_cadence(),
                lookback_days=lookback_days,
                period=self.period_input.value.strip(),
            )
            content = format_review_collection_confirmation(request)
            view = ReviewCollectionConfirmView(self.bot, request)
            if self.message:
                await self.message.edit(content=content, view=view, suppress=True)
            await interaction.response.send_message("Review collection settings updated.", ephemeral=True)

    class ReviewCollectionConfirmView(discord.ui.View):
        def __init__(self, bot: "NewsbotDiscordBot", request: ReviewCollectionRequest):
            super().__init__(timeout=600)
            self.bot = bot
            self.request = request

        @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
        async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            if not await self.bot._require_reviewer(interaction):
                return
            await self.bot.start_review_collection(interaction, self.request)

        @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary)
        async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            if not self.bot.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return
            if not self.bot.is_reviewer(interaction.user.id):
                await interaction.response.send_message("You are not allowed to operate reviewer controls.", ephemeral=True)
                return
            await interaction.response.send_modal(ReviewCollectionModal(self.bot, self.request, interaction.message))

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            if not await self.bot._require_reviewer(interaction):
                return
            await interaction.response.edit_message(content="Manual review collection cancelled.", view=None)

    class EditDraftModal(discord.ui.Modal, title="Edit Review Draft"):
        def __init__(self, bot: "NewsbotDiscordBot", defaults: DraftModalDefaults):
            super().__init__(timeout=300)
            self.bot = bot
            self.article_id = defaults.id

            meta_default = defaults.category
            if defaults.tags:
                meta_default = f"{meta_default} | {', '.join(defaults.tags)}".strip()

            self.title_input = discord.ui.TextInput(label="Title", default=defaults.title[:200], max_length=200)
            self.body_input = discord.ui.TextInput(
                label="Body",
                default=defaults.body[:1500],
                style=discord.TextStyle.paragraph,
                max_length=1500,
            )
            self.source_url_input = discord.ui.TextInput(
                label="Source URL",
                default=defaults.source_url[:500],
                max_length=500,
            )
            self.meta_input = discord.ui.TextInput(
                label="Category | tags",
                default=meta_default,
                required=False,
                max_length=200,
            )
            self.note_input = discord.ui.TextInput(
                label="Reviewer note",
                required=False,
                style=discord.TextStyle.paragraph,
                max_length=300,
            )
            self.add_item(self.title_input)
            self.add_item(self.body_input)
            self.add_item(self.source_url_input)
            self.add_item(self.meta_input)
            self.add_item(self.note_input)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            if not self.bot.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return
            if not self.bot.is_reviewer(interaction.user.id):
                await interaction.response.send_message("You are not allowed to operate reviewer controls.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            category, tags = _parse_meta(self.meta_input.value)
            source_url = self.source_url_input.value.strip()
            if not source_url:
                await interaction.followup.send("Source URL is required.", ephemeral=True)
                return
            try:
                updated = await self.bot.db_call(
                    self.bot.store.update_draft_text,
                    self.article_id,
                    self.title_input.value.strip(),
                    self.body_input.value.strip(),
                    str(interaction.user.id),
                    category_primary=category,
                    tags=tags,
                    reviewer_note=self.note_input.value.strip() or None,
                    source_url=source_url,
                )
            except KeyError:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            except Exception as exc:  # noqa: BLE001 - Discord modal should report DB uniqueness failures.
                await interaction.followup.send(f"Could not update draft: {exc}", ephemeral=True)
                return
            await interaction.followup.send(
                "Review draft updated.\n\n" + render_review_message(updated),
                view=self.bot.review_view_for_draft(updated),
                ephemeral=True,
            )
            self.bot.schedule_refresh_review_message(self.article_id)

    class NewsbotDiscordBot(discord.Client):
        def __init__(self, *, store: NewsbotStore, settings: DiscordSettings):
            intents = discord.Intents.none()
            intents.guilds = True
            super().__init__(intents=intents)
            self.store = store
            self.settings = settings
            self.tree = discord.app_commands.CommandTree(self)
            self.discord_message_interval_seconds = parse_message_interval(
                os.getenv("NEWSBOT_DISCORD_MESSAGE_INTERVAL_SECONDS", "1")
            )
            self._discord_send_lock = asyncio.Lock()
            self._last_discord_send_at = 0.0
            self._review_collection_task: asyncio.Task | None = None

        def is_reviewer(self, user_id: int) -> bool:
            return str(user_id) in self.settings.reviewer_user_ids

        def is_allowed_guild(self, interaction: discord.Interaction) -> bool:
            if not self.settings.allowed_guild_id:
                return True
            return str(interaction.guild_id or "") == self.settings.allowed_guild_id

        async def setup_hook(self) -> None:
            self._register_app_commands()
            review_drafts = await self.db_call(self.store.list_review_message_drafts)
            for draft in review_drafts:
                if draft.review_message_id:
                    self.add_view(self.review_view_for_draft(draft), message_id=int(draft.review_message_id))
            published_drafts = await self.db_call(self.store.list_published_message_drafts)
            for draft in published_drafts:
                if draft.published_message_id:
                    count = await self.db_call(self.store.feedback_count, draft.id)
                    self.add_view(FeedbackView(draft.id, count), message_id=int(draft.published_message_id))
            if self.settings.allowed_guild_id:
                guild = discord.Object(id=int(self.settings.allowed_guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()

        def review_view_for_draft(self, draft) -> ReviewActionsView:
            if draft.status == "ready_weekly":
                return FinalWeeklyView(draft.id)
            if draft.status == "final_ready":
                return FinalSelectedView(draft.id)
            if draft.status == "approved_weekly":
                return ReviewActionsView(draft.id, include_cancel=True)
            if draft.status == "held":
                return ReviewActionsView(draft.id, exclude={"hold"})
            if draft.status == "rejected":
                return ReviewActionsView(draft.id, exclude={"reject"})
            if draft.status == "published":
                return ReviewActionsView(draft.id, exclude={"weekly", "breaking", "hold", "reject"})
            return ReviewActionsView(draft.id)

        def _register_app_commands(self) -> None:
            self.tree.command(
                name="newsbot_next_weekly",
                description="Show the next weekly publish time.",
            )(self._cmd_next_weekly)
            self.tree.command(
                name="newsbot_weekly_queue",
                description="Show drafts approved for the next weekly publish.",
            )(self._cmd_weekly_queue)
            self.tree.command(
                name="newsbot_held",
                description="Show held drafts.",
            )(self._cmd_held)
            self.tree.command(
                name="newsbot_rejected",
                description="Show rejected drafts.",
            )(self._cmd_rejected)
            self.tree.command(
                name="newsbot_cancelled",
                description="Show drafts whose weekly approval was cancelled.",
            )(self._cmd_cancelled)
            self.tree.command(
                name="newsbot_report",
                description="Show a compact Newsbot database test report.",
            )(self._cmd_report)
            self.tree.command(
                name="newsbot_refresh_reviews",
                description="Refresh stored review messages with current buttons and content.",
            )(self._cmd_refresh_reviews)
            self.tree.command(
                name="newsbot_prepare_weekly",
                description="Post final weekly review messages now.",
            )(self._cmd_prepare_weekly)
            self.tree.command(
                name="newsbot_collect_reviews",
                description="Manually collect and submit review candidate news.",
            )(self._cmd_collect_reviews)
            self.tree.command(
                name="newsbot_record_missed",
                description="Investigate and register one missed Lab Automation URL.",
            )(self._cmd_record_missed)

        async def on_ready(self) -> None:
            print(f"Discord bot connected as {self.user}")

        async def send_channel_message(self, channel, *args, **kwargs):
            async with self._discord_send_lock:
                if self.discord_message_interval_seconds > 0:
                    elapsed = monotonic() - self._last_discord_send_at
                    remaining = self.discord_message_interval_seconds - elapsed
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                message = await channel.send(*args, **kwargs)
                self._last_discord_send_at = monotonic()
                return message

        async def on_interaction(self, interaction: discord.Interaction) -> None:
            if interaction.type != discord.InteractionType.component:
                return
            if not interaction.data:
                return

            custom_id = str(interaction.data.get("custom_id") or "")
            if custom_id.startswith("nb:review:weekly:"):
                await self._handle_weekly(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:ready_go:"):
                await self._handle_ready_go(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:publish_digest:"):
                await self._handle_publish_digest(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:reorder_digest:"):
                await self._handle_reorder_digest(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:priority_top:"):
                await self._handle_priority(interaction, custom_id.rsplit(":", 1)[-1], "priority_top")
            elif custom_id.startswith("nb:review:priority_up:"):
                await self._handle_priority(interaction, custom_id.rsplit(":", 1)[-1], "priority_up")
            elif custom_id.startswith("nb:review:priority_down:"):
                await self._handle_priority(interaction, custom_id.rsplit(":", 1)[-1], "priority_down")
            elif custom_id.startswith("nb:review:edit:"):
                await self._handle_edit(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:check_source:"):
                await self._handle_check_source(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:breaking:"):
                await self._handle_breaking(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:cancel_weekly:"):
                await self._handle_cancel_weekly(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:cancel_publish:"):
                await self._handle_cancel_publish(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:hold:"):
                await self._handle_hold(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:review:reject:"):
                await self._handle_reject(interaction, custom_id.rsplit(":", 1)[-1])
            elif custom_id.startswith("nb:feedback:interested:"):
                await self._handle_feedback(interaction, custom_id.rsplit(":", 1)[-1])

        async def refresh_review_message(self, article_id: str) -> None:
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None or not draft.review_channel_id or not draft.review_message_id:
                return
            channel = self.get_channel(int(draft.review_channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(draft.review_channel_id))
            message = await channel.fetch_message(int(draft.review_message_id))
            await message.edit(content=render_review_message(draft), view=self.review_view_for_draft(draft), suppress=True)

        async def refresh_all_review_messages(self) -> tuple[int, int]:
            drafts = await self.db_call(self.store.list_review_message_drafts)
            refreshed = 0
            failed = 0
            for draft in drafts:
                try:
                    await self.refresh_review_message(draft.id)
                except Exception as exc:  # noqa: BLE001 - keep refreshing other messages even if one is gone.
                    failed += 1
                    print(f"newsbot: failed to refresh review message {draft.id}: {exc}")
                else:
                    refreshed += 1
            return refreshed, failed

        def schedule_refresh_review_message(self, article_id: str) -> None:
            task = asyncio.create_task(self.refresh_review_message(article_id))
            task.add_done_callback(self._log_background_error)

        def schedule_db_call(self, func, *args, **kwargs) -> None:
            task = asyncio.create_task(self.db_call(func, *args, **kwargs))
            task.add_done_callback(self._log_background_error)

        def _log_background_error(self, task: asyncio.Task) -> None:
            try:
                task.result()
            except Exception as exc:  # noqa: BLE001 - background tasks should not disappear silently.
                print(f"newsbot: background task failed: {exc}")

        async def db_call(self, func, *args, **kwargs):
            return await asyncio.to_thread(func, *args, **kwargs)

        async def _record_editorial(self, article_id: str, user_id: str, decision: str, reason_code: str, note: str) -> None:
            await self.db_call(
                self.store.record_editorial_judgment,
                article_id=article_id,
                reviewer_user_id=user_id,
                decision=decision,
                reason_code=reason_code,
                note=note,
            )

        async def _require_reviewer(self, interaction: discord.Interaction) -> bool:
            if not self.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return False
            if not self.is_reviewer(interaction.user.id):
                await interaction.response.send_message("You are not allowed to operate reviewer controls.", ephemeral=True)
                return False
            return True

        def _format_draft_list(self, title: str, drafts) -> str:
            if not drafts:
                return f"{title}\nNo drafts found."
            lines = [title]
            for index, draft in enumerate(drafts, start=1):
                title_text = draft.title_edited or draft.title_original
                suffix = f" | scheduled {draft.scheduled_at}" if draft.scheduled_at else ""
                lines.append(f"{index}. [{draft.short_id}] {title_text}{suffix}")
            return "\n".join(lines)

        async def _defer_admin_command(self, interaction: discord.Interaction) -> bool:
            if not await self._require_reviewer(interaction):
                return False
            await interaction.response.defer(ephemeral=True, thinking=True)
            return True

        async def _cmd_next_weekly(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            await interaction.followup.send(
                f"Next weekly publish: {format_schedule(next_weekly_publish_at())}",
                ephemeral=True,
            )

        async def _cmd_weekly_queue(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            drafts = await self.db_call(
                self.store.list_approved_weekly,
                default_review_topic(),
                default_review_cadence(),
                20,
            )
            await interaction.followup.send(
                self._format_draft_list("Approved weekly drafts:", drafts),
                ephemeral=True,
            )

        async def _cmd_held(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            drafts = await self.db_call(self.store.list_drafts_by_status, "held", limit=20)
            await interaction.followup.send(self._format_draft_list("Held drafts:", drafts), ephemeral=True)

        async def _cmd_rejected(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            drafts = await self.db_call(self.store.list_drafts_by_status, "rejected", limit=20)
            await interaction.followup.send(self._format_draft_list("Rejected drafts:", drafts), ephemeral=True)

        async def _cmd_cancelled(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            drafts = await self.db_call(self.store.list_recent_cancelled_weekly, 20)
            await interaction.followup.send(
                self._format_draft_list("Cancelled weekly approvals:", drafts),
                ephemeral=True,
            )

        async def _cmd_report(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            counts = await self.db_call(self.store.table_counts)
            lines = ["Newsbot database report:"]
            lines.extend(f"- {table}: {count}" for table, count in counts.items())
            lines.append(f"- next_weekly_publish: {format_schedule(next_weekly_publish_at())}")
            await interaction.followup.send("\n".join(lines), ephemeral=True)

        async def _cmd_refresh_reviews(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            refreshed, failed = await self.refresh_all_review_messages()
            await interaction.followup.send(
                f"Review messages refreshed: refreshed={refreshed}, failed={failed}",
                ephemeral=True,
            )

        async def _cmd_prepare_weekly(self, interaction: discord.Interaction) -> None:
            if not await self._defer_admin_command(interaction):
                return
            try:
                prepared, failed, digest_preview = await self.prepare_weekly_publish(
                    default_review_topic(),
                    default_review_cadence(),
                )
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(f"Could not prepare weekly final review: {exc}", ephemeral=True)
                return
            await interaction.followup.send(
                "Weekly final review prepared: "
                + f"prepared={prepared}, failed={failed}, digest_preview={digest_preview}",
                ephemeral=True,
            )

        async def _cmd_collect_reviews(self, interaction: discord.Interaction) -> None:
            if not await self._require_reviewer(interaction):
                return
            request = ReviewCollectionRequest.default()
            await interaction.response.send_message(
                format_review_collection_confirmation(request),
                view=ReviewCollectionConfirmView(self, request),
                ephemeral=True,
            )

        async def _cmd_record_missed(self, interaction: discord.Interaction, url: str) -> None:
            if not await self._defer_admin_command(interaction):
                return
            try:
                result = await self.db_call(research_missed_item_with_codex, url)
                if not result.relevant:
                    await interaction.followup.send(
                        format_missed_item_result(result, dry_run=True)
                        + "\n\nThe URL was not registered because its Lab Automation relevance could not be verified.",
                        ephemeral=True,
                    )
                    return
                judgment_id = await self.db_call(
                    record_researched_missed_item,
                    self.store,
                    result,
                    reviewer_user_id=str(interaction.user.id),
                    topic=default_review_topic(),
                    cadence=default_review_cadence(),
                )
            except Exception as exc:  # noqa: BLE001 - report investigation failures to the requesting admin.
                await interaction.followup.send(
                    f"Could not investigate/register the missed URL: {exc}",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                format_missed_item_result(result, judgment_id=judgment_id),
                ephemeral=True,
            )

        async def start_review_collection(
            self,
            interaction: discord.Interaction,
            request: ReviewCollectionRequest,
        ) -> None:
            if self._review_collection_task and not self._review_collection_task.done():
                await interaction.response.send_message(
                    "Review candidate collection is already running. Check the current status message.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not self.settings.review_channel_id:
                await interaction.followup.send("DISCORD_REVIEW_CHANNEL_ID is not set.", ephemeral=True)
                return

            channel = self.get_channel(int(self.settings.review_channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(self.settings.review_channel_id))

            started_at = datetime.now(WEEKLY_PUBLISH_TZ)
            command = build_review_collection_command(
                request,
                db_path=self.store.path,
                config_path=DEFAULT_CONFIG_PATH,
            )
            status_message = await self.send_channel_message(
                channel,
                format_review_collection_status(
                    request,
                    status="starting",
                    started_at=started_at,
                    command=command,
                    output_lines=[],
                ),
                suppress_embeds=True,
            )
            self._review_collection_task = asyncio.create_task(
                self._run_review_collection_process(request, command, started_at, status_message)
            )
            self._review_collection_task.add_done_callback(self._log_background_error)
            await interaction.followup.send(
                f"Started review candidate collection. Status: {status_message.jump_url}",
                ephemeral=True,
            )

        async def _run_review_collection_process(
            self,
            request: ReviewCollectionRequest,
            command: list[str],
            started_at: datetime,
            status_message,
        ) -> None:
            output_lines: list[str] = []
            last_edit_at = 0.0
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            async def edit_status(status: str, returncode: int | None = None, *, force: bool = False) -> None:
                nonlocal last_edit_at
                current = monotonic()
                if not force and current - last_edit_at < STATUS_UPDATE_INTERVAL_SECONDS:
                    return
                last_edit_at = current
                try:
                    await status_message.edit(
                        content=format_review_collection_status(
                            request,
                            status=status,
                            started_at=started_at,
                            command=command,
                            output_lines=output_lines,
                            returncode=returncode,
                        ),
                        suppress=True,
                    )
                except Exception as exc:  # noqa: BLE001 - status edits should not stop the subprocess.
                    print(f"newsbot: failed to update review collection status: {exc}")

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except Exception as exc:  # noqa: BLE001
                output_lines.append(f"Could not start process: {exc}")
                await edit_status("failed to start", returncode=1, force=True)
                return

            await edit_status("running", force=True)
            assert process.stdout is not None
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                output_lines.append(line)
                if len(output_lines) > 80:
                    del output_lines[: len(output_lines) - 80]
                await edit_status("running")

            returncode = await process.wait()
            status = "completed" if returncode == 0 else "failed"
            await edit_status(status, returncode=returncode, force=True)

        async def prepare_weekly_publish(self, topic: str, cadence: str) -> tuple[int, int, int]:
            channel = self.get_channel(int(self.settings.review_channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(self.settings.review_channel_id))

            final_ready_drafts = await self.db_call(self.store.list_final_ready_weekly, topic, cadence)
            ready_drafts = await self.db_call(self.store.list_ready_weekly, topic, cadence)
            approved_drafts = await self.db_call(self.store.list_approved_weekly, topic, cadence)
            drafts = ready_drafts + approved_drafts

            if final_ready_drafts and not drafts:
                content = (
                    "Final digest is ready.\n"
                    "Review the order below. Use Reorder if needed, then click Publish Digest.\n\n"
                    + render_weekly_digest_overview(final_ready_drafts)
                )
                await self.send_channel_message(
                    channel,
                    content,
                    view=DigestPublishView(final_ready_drafts[0].id),
                    suppress_embeds=True,
                )
                return 0, 0, 1

            prepared = 0
            failed = 0
            for index, draft in enumerate(drafts, start=1):
                try:
                    updated = await self.db_call(
                        self.store.set_draft_status,
                        draft.id,
                        "ready_weekly",
                        "system",
                        publish_type="weekly",
                        scheduled_at=draft.scheduled_at or None,
                    )
                    content = (
                        f"Final Weekly Review #{index}\n"
                        "Choose Publish, Edit Draft, or Cancel. After all items are judged, "
                        "Reorder and Publish Digest will appear.\n\n"
                        + render_review_message(updated)
                    )
                    message = await self.send_channel_message(
                        channel,
                        content,
                        view=FinalWeeklyView(updated.id),
                        suppress_embeds=True,
                    )
                    await self.db_call(self.store.mark_review_message, updated.id, str(channel.id), str(message.id))
                    prepared += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(f"newsbot: prepare weekly failed for draft {draft.id}: {exc}")
            return prepared, failed, 0

        async def _handle_weekly(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "weekly"))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            if draft.status == "published":
                await interaction.followup.send("This draft cannot be approved for weekly publishing.", ephemeral=True)
                return
            scheduled_at = next_weekly_publish_at()
            updated = await self.db_call(
                self.store.set_draft_status,
                article_id,
                "approved_weekly",
                str(interaction.user.id),
                publish_type="weekly",
                scheduled_at=format_schedule(scheduled_at),
            )
            await self._record_editorial(article_id, str(interaction.user.id), "accepted", reason_code, reason_note)
            await interaction.followup.send(
                "Draft approved for weekly publishing.\n"
                f"Scheduled for: {format_schedule(scheduled_at)}\n\n"
                + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            self.schedule_refresh_review_message(article_id)

        async def _handle_cancel_weekly(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "cancel_weekly"))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            if draft.status not in {"approved_weekly", "ready_weekly"}:
                await interaction.followup.send("This draft is not approved for weekly publishing.", ephemeral=True)
                return
            updated = await self.db_call(
                self.store.set_draft_status,
                article_id,
                "pending",
                str(interaction.user.id),
            )
            await self.db_call(
                self.store.record_review_action,
                article_id,
                str(interaction.user.id),
                "cancel_weekly",
                draft.to_dict(),
                updated.to_dict(),
            )
            await self._record_editorial(article_id, str(interaction.user.id), "delivery_cancelled", reason_code, reason_note)
            await interaction.followup.send(
                "Weekly approval cancelled.\n\n" + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            self.schedule_refresh_review_message(article_id)

        async def _handle_priority(self, interaction: discord.Interaction, article_id: str, action_type: str) -> None:
            if not await self._require_reviewer(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                updated = await self.db_call(
                    self.store.adjust_draft_priority,
                    article_id,
                    str(interaction.user.id),
                    action_type,
                )
            except KeyError:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            await interaction.followup.send(
                f"Priority updated. Current score: {updated.importance_score:.2f}\n\n"
                + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            if interaction.message:
                await interaction.message.edit(
                    content="Final Weekly Review\n"
                    "Choose Publish, Edit Draft, or Cancel. After all items are judged, Reorder and Publish Digest will appear.\n\n"
                    + render_review_message(updated),
                    view=self.review_view_for_draft(updated),
                    suppress=True,
                )

        async def _maybe_post_digest_ready_message(
            self,
            interaction: discord.Interaction,
            topic: str,
            cadence: str,
            scheduled_at: str,
        ) -> None:
            pending = await self.db_call(self.store.list_ready_weekly_batch, topic, cadence, scheduled_at)
            final_ready = await self.db_call(self.store.list_final_ready_weekly_batch, topic, cadence, scheduled_at)
            if pending or not final_ready:
                return
            channel = self.get_channel(int(self.settings.review_channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(self.settings.review_channel_id))
            content = (
                "Final digest is ready.\n"
                "Review the order below. Use Reorder if needed, then click Publish Digest.\n\n"
                + render_weekly_digest_overview(final_ready)
            )
            await self.send_channel_message(
                channel,
                content,
                view=DigestPublishView(final_ready[0].id),
                suppress_embeds=True,
            )

        async def _handle_ready_go(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "ready_go"))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            if draft.status == "published":
                await interaction.followup.send("This draft is already published.", ephemeral=True)
                return
            if draft.status != "ready_weekly":
                await interaction.followup.send("This draft is not waiting for final screening.", ephemeral=True)
                return
            updated = await self.db_call(
                self.store.set_draft_status,
                article_id,
                "final_ready",
                str(interaction.user.id),
                publish_type="weekly",
            )
            await self._record_editorial(article_id, str(interaction.user.id), "accepted", reason_code, reason_note)
            await interaction.followup.send(
                "Selected for the final digest.\n\n" + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            if interaction.message:
                await interaction.message.edit(
                    content="Selected for final digest. Reorder and Publish Digest will appear after all items are judged.\n\n"
                    + render_review_message(updated),
                    view=None,
                    suppress=True,
                )
            await self._maybe_post_digest_ready_message(interaction, updated.topic, updated.cadence, updated.scheduled_at)

        async def _handle_cancel_publish(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "cancel_publish"))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            if draft.status not in {"ready_weekly", "final_ready"}:
                await interaction.followup.send("This draft is not in final weekly review.", ephemeral=True)
                return
            should_check_completion = draft.status == "ready_weekly"
            updated = await self.db_call(self.store.set_draft_status, article_id, "pending", str(interaction.user.id))
            await self.db_call(
                self.store.record_review_action,
                article_id,
                str(interaction.user.id),
                "cancel_publish",
                draft.to_dict(),
                updated.to_dict(),
            )
            await self._record_editorial(article_id, str(interaction.user.id), "delivery_cancelled", reason_code, reason_note)
            await interaction.followup.send(
                "Cancelled from final publish. The draft returned to pending review.\n\n" + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            if interaction.message:
                await interaction.message.edit(
                    content="Publish cancelled for this item.\n\n" + render_review_message(updated),
                    view=None,
                    suppress=True,
                )
            if should_check_completion:
                await self._maybe_post_digest_ready_message(interaction, draft.topic, draft.cadence, draft.scheduled_at)

        async def _publish_weekly_digest(
            self,
            interaction: discord.Interaction,
            topic: str,
            cadence: str,
            scheduled_at: str,
        ) -> int:
            drafts = await self.db_call(self.store.list_final_ready_weekly_batch, topic, cadence, scheduled_at)
            if not drafts:
                return 0

            channel = self.get_channel(int(self.settings.publish_channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(self.settings.publish_channel_id))

            await self.send_channel_message(channel, render_weekly_digest_overview(drafts), suppress_embeds=True)

            published_count = 0
            for index, queued_draft in enumerate(order_weekly_drafts(drafts), start=1):
                before = await self.db_call(self.store.get_draft, queued_draft.id)
                message_text = render_weekly_detail_message(queued_draft, index, leading_gap=index == 1)
                published = await self.send_channel_message(
                    channel,
                    message_text,
                    view=FeedbackView(queued_draft.id, 0),
                    suppress_embeds=True,
                )
                await self.db_call(
                    self.store.mark_published,
                    queued_draft.id,
                    str(channel.id),
                    str(published.id),
                    "weekly",
                )
                latest = await self.db_call(self.store.get_draft, queued_draft.id)
                if before is not None and latest is not None:
                    await self.db_call(
                        self.store.record_review_action,
                        queued_draft.id,
                        str(interaction.user.id),
                        "publish_weekly",
                        before.to_dict(),
                        latest.to_dict(),
                    )
                await self.db_call(
                    self.store.record_delivery_event,
                    article_id=queued_draft.id,
                    topic=queued_draft.topic,
                    cadence=queued_draft.cadence,
                    publish_type="weekly",
                    destination="discord",
                    status="sent",
                    discord_channel_id=str(channel.id),
                    discord_message_id=str(published.id),
                    message_text=message_text,
                )
                published_count += 1
                self.schedule_refresh_review_message(queued_draft.id)
            return published_count

        async def _handle_publish_digest(self, interaction: discord.Interaction, article_id: str) -> None:
            if not await self._require_reviewer(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            pending = await self.db_call(self.store.list_ready_weekly_batch, draft.topic, draft.cadence, draft.scheduled_at)
            if pending:
                await interaction.followup.send(
                    f"Cannot publish yet. {len(pending)} final review item(s) still need Publish or Cancel.",
                    ephemeral=True,
                )
                return
            published_count = await self._publish_weekly_digest(interaction, draft.topic, draft.cadence, draft.scheduled_at)
            if published_count == 0:
                await interaction.followup.send("No published items selected.", ephemeral=True)
                return

            if interaction.message:
                await interaction.message.edit(
                    content=f"Weekly digest published. Published {published_count} items.",
                    view=None,
                    suppress=True,
                )
            await interaction.followup.send(f"Weekly digest published. Published {published_count} items.", ephemeral=True)

        async def _handle_reorder_digest(self, interaction: discord.Interaction, article_id: str) -> None:
            if not await self._require_reviewer(interaction):
                return
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.response.send_message("Draft not found.", ephemeral=True)
                return
            if draft.status != "final_ready":
                await interaction.response.send_message("This draft is not in the final digest.", ephemeral=True)
                return
            await interaction.response.send_modal(
                ReorderDigestModal(self, draft.topic, draft.cadence, draft.scheduled_at, interaction.message)
            )

        async def _handle_edit(self, interaction: discord.Interaction, article_id: str) -> None:
            if not await self._require_reviewer(interaction):
                return
            defaults = parse_review_message_for_modal(
                article_id,
                interaction.message.content if interaction.message else "",
            )
            await interaction.response.send_modal(EditDraftModal(self, defaults))

        def _format_source_check_result(self, result: SourceCheckResult) -> str:
            lines = [
                f"Status: {result.status}",
                f"Current URL reachable: {result.current_url_reachable}",
            ]
            if result.replacement_url:
                lines.append(f"Replacement URL: {result.replacement_url}")
            if result.reason:
                lines.append(f"Reason: {result.reason}")
            return "\n".join(lines)

        async def _handle_check_source(self, interaction: discord.Interaction, article_id: str) -> None:
            if not await self._require_reviewer(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return

            title = draft.title_edited or draft.title_original
            try:
                result = await self.db_call(
                    check_source_with_codex,
                    title=title,
                    current_url=draft.source_url,
                    source_name=draft.source_name,
                )
            except Exception as exc:  # noqa: BLE001 - Codex CLI availability/runtime errors should be user-visible.
                await interaction.followup.send(
                    "Could not check the source URL with Codex CLI.\n"
                    f"Current URL: {draft.source_url}\n"
                    f"Error: {exc}",
                    ephemeral=True,
                )
                return

            updated = draft
            if result.status == "replace" and result.replacement_url and result.replacement_url != draft.source_url:
                try:
                    updated = await self.db_call(
                        self.store.update_draft_source_url,
                        article_id,
                        result.replacement_url,
                        str(interaction.user.id),
                        reason=result.reason or "Updated by Check Source",
                    )
                except Exception as exc:  # noqa: BLE001
                    await interaction.followup.send(
                        "Codex suggested a replacement URL, but the draft could not be updated.\n\n"
                        + self._format_source_check_result(result)
                        + f"\n\nUpdate error: {exc}",
                        ephemeral=True,
                    )
                    return
                self.schedule_refresh_review_message(article_id)

            prefix = "Source URL updated." if updated.source_url != draft.source_url else "Source URL checked."
            await interaction.followup.send(
                prefix + "\n\n" + self._format_source_check_result(result) + "\n\n" + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )

        async def _handle_breaking(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "breaking"))
                return
            await interaction.response.send_message(
                "Do you want to publish this draft as breaking news?",
                view=BreakingConfirmView(self, article_id, reason_code, reason_note),
                ephemeral=True,
            )
            self.schedule_record_breaking_request(article_id, str(interaction.user.id))

        async def _handle_hold(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "hold"))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            if draft.status == "published":
                await interaction.followup.send("This draft cannot be moved to hold.", ephemeral=True)
                return
            updated = await self.db_call(self.store.set_draft_status, article_id, "held", str(interaction.user.id))
            await self._record_editorial(article_id, str(interaction.user.id), "excluded", reason_code, reason_note)
            await interaction.followup.send(
                "Draft moved to hold. You can choose another action below.\n\n" + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            self.schedule_refresh_review_message(article_id)

        async def _handle_reject(self, interaction: discord.Interaction, article_id: str, *, reason_code: str = "", reason_note: str = "") -> None:
            if not await self._require_reviewer(interaction):
                return
            if not reason_code:
                await interaction.response.send_modal(EditorialDecisionModal(self, article_id, "reject"))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            draft = await self.db_call(self.store.get_draft, article_id)
            if draft is None:
                await interaction.followup.send("Draft not found.", ephemeral=True)
                return
            if draft.status == "published":
                await interaction.followup.send("Published drafts cannot be rejected.", ephemeral=True)
                return
            updated = await self.db_call(self.store.set_draft_status, article_id, "rejected", str(interaction.user.id))
            await self._record_editorial(article_id, str(interaction.user.id), "excluded", reason_code, reason_note)
            await interaction.followup.send(
                "Draft rejected. You can choose another action below.\n\n" + render_review_message(updated),
                view=self.review_view_for_draft(updated),
                ephemeral=True,
            )
            self.schedule_refresh_review_message(article_id)

        async def _handle_feedback(self, interaction: discord.Interaction, article_id: str) -> None:
            if not self.is_allowed_guild(interaction):
                await interaction.response.send_message("Interactions from this guild are not allowed.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            inserted = await self.db_call(
                record_interested_feedback,
                self.store,
                article_id=article_id,
                user_id=str(interaction.user.id),
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=str(interaction.message.id) if interaction.message else None,
            )
            count = await self.db_call(self.store.feedback_count, article_id)
            if interaction.message:
                await interaction.message.edit(view=FeedbackView(article_id, count), suppress=True)
            if inserted:
                await interaction.followup.send("Recorded.", ephemeral=True)
            else:
                await interaction.followup.send("Already recorded.", ephemeral=True)

        def schedule_record_breaking_request(self, article_id: str, reviewer_user_id: str) -> None:
            async def record() -> None:
                draft = await self.db_call(self.store.get_draft, article_id)
                if draft is None:
                    return
                await self.db_call(
                    self.store.record_review_action,
                    article_id,
                    reviewer_user_id,
                    "breaking_request",
                    draft.to_dict(),
                )

            task = asyncio.create_task(record())
            task.add_done_callback(self._log_background_error)


async def _run_bot_async(db_path: str) -> None:
    if discord is None:
        raise RuntimeError(
            "discord.py is not installed. Install project dependencies, including discord.py>=2.4.0."
        ) from DISCORD_IMPORT_ERROR

    settings = load_discord_settings()
    if not settings.bot_token:
        raise ValueError("DISCORD_BOT_TOKEN is not set")
    if not settings.review_channel_id:
        raise ValueError("DISCORD_REVIEW_CHANNEL_ID is not set")
    if not settings.publish_channel_id:
        raise ValueError("DISCORD_PUBLISH_CHANNEL_ID is not set")

    store = NewsbotStore(db_path)
    store.init()
    bot = NewsbotDiscordBot(store=store, settings=settings)

    discord.utils.setup_logging()

    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()

    def request_shutdown() -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        print("Discord bot shutdown requested", flush=True)
        if not bot.is_closed():
            asyncio.create_task(bot.close())

    registered_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, request_shutdown)
        except (NotImplementedError, RuntimeError):
            continue
        registered_signals.append(signum)

    try:
        await bot.start(settings.bot_token)
    finally:
        for signum in registered_signals:
            loop.remove_signal_handler(signum)
        if not bot.is_closed():
            await bot.close()


def run_bot(db_path: str) -> None:
    asyncio.run(_run_bot_async(db_path))
