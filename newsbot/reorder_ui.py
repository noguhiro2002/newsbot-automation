from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from .models import ArticleDraft
from .render import order_weekly_drafts, render_weekly_digest_overview, truncate, weekly_digest_section

if TYPE_CHECKING:
    from .discord_bot import NewsbotDiscordBot


logger = logging.getLogger(__name__)
PAGE_SIZE = 10
REOPEN_NOTICE = "記事や並び順が変更されています。保存せず終了しました。Reorder を開き直してください。"


class ReorderDigestView(discord.ui.View):
    """A private, disposable ordering session; only Save writes to the store."""

    def __init__(
        self, bot: NewsbotDiscordBot, drafts: list[ArticleDraft], user_id: int,
        source_message: discord.Message | None,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.snapshot = list(drafts)
        self.drafts = {draft.id: draft for draft in drafts}
        self.ordered_ids = [draft.id for draft in order_weekly_drafts(drafts)]
        self.initial_ids = self.ordered_ids.copy()
        self.user_id = user_id
        self.source_message = source_message
        self.message: discord.WebhookMessage | None = None
        self.page = 0
        self.selected_id = self.ordered_ids[0] if self.ordered_ids else None
        self.finished = False
        self._lock = asyncio.Lock()
        self.article_select = discord.ui.Select(placeholder="移動する記事を選択", row=0)
        self.article_select.callback = self.choose_article
        self.add_item(self.article_select)
        self.refresh_controls()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.ordered_ids) + PAGE_SIZE - 1) // PAGE_SIZE)

    def selected_bounds(self) -> tuple[int, int, int] | None:
        if self.selected_id is None:
            return None
        index = self.ordered_ids.index(self.selected_id)
        section = weekly_digest_section(self.drafts[self.selected_id])
        indices = [i for i, article_id in enumerate(self.ordered_ids)
                   if weekly_digest_section(self.drafts[article_id]) == section]
        return index, indices[0], indices[-1]

    def move_selected(self, direction: str) -> None:
        bounds = self.selected_bounds()
        if bounds is None:
            return
        index, first, last = bounds
        target = {"up": max(first, index - 1), "down": min(last, index + 1),
                  "first": first, "last": last}[direction]
        self.ordered_ids.insert(target, self.ordered_ids.pop(index))
        self.page = target // PAGE_SIZE

    def refresh_controls(self) -> None:
        start = self.page * PAGE_SIZE
        self.article_select.options = [
            discord.SelectOption(
                label=truncate(f"{index + 1}. {' '.join((draft.title_edited or draft.title_original).split())}", 100),
                value=draft.id, description=f"{weekly_digest_section(draft)} · ID: {draft.short_id}",
                default=draft.id == self.selected_id,
            )
            for index, article_id in enumerate(self.ordered_ids[start:start + PAGE_SIZE], start=start)
            for draft in [self.drafts[article_id]]
        ] or [discord.SelectOption(label="並べ替える記事がありません", value="empty")]
        self.article_select.disabled = not self.ordered_ids
        bounds = self.selected_bounds()
        self.up.disabled = self.first.disabled = bounds is None or bounds[0] == bounds[1]
        self.down.disabled = self.last.disabled = bounds is None or bounds[0] == bounds[2]
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page == self.page_count - 1
        self.save.disabled = self.ordered_ids == self.initial_ids

    def preview(self) -> dict:
        embed = discord.Embed(colour=discord.Colour.blurple())
        if self.selected_id:
            draft = self.drafts[self.selected_id]
            index = self.ordered_ids.index(self.selected_id) + 1
            embed.title = f"選択中: {index}. ({weekly_digest_section(draft)})"
            embed.description = discord.utils.escape_markdown(draft.title_edited or draft.title_original)
        else:
            embed.title = "並べ替える記事がありません"
        start = self.page * PAGE_SIZE
        for section in ("News", "Events"):
            lines = []
            for index, article_id in enumerate(self.ordered_ids[start:start + PAGE_SIZE], start=start + 1):
                draft = self.drafts[article_id]
                if weekly_digest_section(draft) != section:
                    continue
                title = " ".join((draft.title_edited or draft.title_original).split())
                title = truncate(discord.utils.escape_markdown(title), 70)
                marker = "▶ " if article_id == self.selected_id else ""
                lines.append(f"{marker}{index}. {title}")
            if lines:
                embed.add_field(name=section, value="\n".join(lines), inline=False)
        embed.set_footer(text=f"{self.page + 1} / {self.page_count} ページ · 全{len(self.ordered_ids)}件")
        return {
            "content": "**ダイジェストの並べ替え**\n記事を選び、ボタンで移動してください。News / Events の区分内で移動します。\n変更は「保存」で確定します。10分間操作がないと破棄されます。",
            "embed": embed,
            "view": self,
            "allowed_mentions": discord.AllowedMentions.none(),
        }

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.bot._require_reviewer(interaction):
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("自分で Reorder を開いて操作してください。", ephemeral=True)
            return False
        if self.finished:
            await interaction.response.send_message("この並べ替え画面は終了しています。Reorder を開き直してください。", ephemeral=True)
            return False
        return True

    async def operate(self, interaction: discord.Interaction, action: str, article_id: str | None = None) -> None:
        # Defer as a component update: ephemeral messages must use the
        # interaction webhook, never interaction.message.edit (channel API).
        await interaction.response.defer(thinking=False)
        async with self._lock:
            if self.finished:
                await interaction.followup.send("この画面の操作は終了しています。", ephemeral=True)
                return
            if action == "save":
                async with self.bot._digest_order_lock:
                    await self.save_order(interaction)
                return
            if action == "cancel":
                self.finished = True
                self.stop()
                await self.finish_message(interaction, "並べ替えをキャンセルしました。変更は保存していません。")
                return
            if action == "select":
                if article_id not in self.ordered_ids[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]:
                    await interaction.followup.send("現在のページから記事を選んでください。", ephemeral=True)
                    return
                self.selected_id = article_id
            elif action in {"previous", "next"}:
                self.page = max(0, min(self.page_count - 1, self.page + (-1 if action == "previous" else 1)))
                self.selected_id = self.ordered_ids[self.page * PAGE_SIZE] if self.ordered_ids else None
            else:
                self.move_selected(action)
            self.refresh_controls()
            await interaction.edit_original_response(**self.preview())

    async def choose_article(self, interaction: discord.Interaction) -> None:
        # Read this interaction's values; Select.values can be replaced by a
        # second tap while the first callback is still waiting.
        values = (interaction.data or {}).get("values", [])
        await self.operate(interaction, "select", values[0] if values else None)

    async def finish_message(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(content=content, embed=None, view=None)
        except Exception:
            logger.warning("Could not update private digest reorder message", exc_info=True)
            await interaction.followup.send(content, ephemeral=True)

    async def save_order(self, interaction: discord.Interaction) -> None:
        if self.ordered_ids == self.initial_ids:
            await interaction.followup.send("並び順は変更されていません。", ephemeral=True)
            return
        self.finished = True
        batch = self.snapshot[0]
        try:
            updated = await self.bot.db_call(
                self.bot.store.reorder_final_ready_weekly,
                batch.topic, batch.cadence, self.ordered_ids.copy(), str(interaction.user.id), batch.scheduled_at,
                expected_drafts=self.snapshot,
            )
        except ValueError:
            self.stop()
            await self.finish_message(interaction, REOPEN_NOTICE)
            return
        except Exception:
            logger.exception("Could not save digest order")
            self.finished = False
            await interaction.followup.send("保存できませんでした。時間をおいて「保存」を押してください。", ephemeral=True)
            return

        # The transaction has committed. Stop before any message updates so
        # failures here can never cause the user to submit the same save twice.
        self.stop()
        confirmation = "並び順を保存しました。"
        if self.source_message:
            from .discord_bot import DigestPublishView

            try:
                await self.source_message.edit(
                    content="Final digest is ready.\n"
                    "Review the order below. Use Reorder if needed, then click Publish Digest.\n\n"
                    + render_weekly_digest_overview(updated),
                    view=DigestPublishView(updated[0].id), suppress=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                logger.warning("Saved digest order, but could not refresh the shared preview", exc_info=True)
                confirmation += "確認メッセージは更新できませんでした。Preview Digest で確認してください。再保存は不要です。"
        await self.finish_message(interaction, confirmation)

    @discord.ui.button(label="↑ 上へ", style=discord.ButtonStyle.primary, row=1)
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "up")

    @discord.ui.button(label="↓ 下へ", style=discord.ButtonStyle.primary, row=1)
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "down")

    @discord.ui.button(label="先頭へ", row=1)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "first")

    @discord.ui.button(label="末尾へ", row=1)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "last")

    @discord.ui.button(label="前のページ", row=2)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "previous")

    @discord.ui.button(label="次のページ", row=2)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "next")

    @discord.ui.button(label="保存", style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "save")

    @discord.ui.button(label="キャンセル", row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.operate(interaction, "cancel")

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.finished:
                return
            self.finished = True
            self.stop()
            if self.message:
                try:
                    # This is the WebhookMessage returned by followup.send,
                    # not the regular Message attached to a component event.
                    await self.message.edit(
                        content="並べ替えは時間切れになりました。変更は保存していません。Reorder を開き直してください。",
                        embed=None, view=None,
                    )
                except discord.HTTPException:
                    logger.warning("Could not expire private digest reorder message", exc_info=True)
