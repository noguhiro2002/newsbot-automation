from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from .draft_rewrite import DraftRewriteOptions, generate_draft_rewrites, rewrite_model
from .models import ArticleDraft

if TYPE_CHECKING:
    from .discord_bot import NewsbotDiscordBot


logger = logging.getLogger(__name__)


class RewriteDraftModal(discord.ui.Modal, title="Generate Draft Options"):
    def __init__(self, bot: NewsbotDiscordBot, article_id: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.article_id = article_id
        self.model_input = discord.ui.TextInput(
            label="Rewrite model", default=rewrite_model() or None,
            placeholder="Model for rewriting (separate from collection)", max_length=100,
        )
        self.instructions_input = discord.ui.TextInput(
            label="Writing preferences (optional)", style=discord.TextStyle.paragraph,
            placeholder="例: 直訳調を直し、主語と述語を明確にしてください。",
            required=False, max_length=500,
        )
        self.add_item(self.model_input)
        self.add_item(self.instructions_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.bot._require_reviewer(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.article_id in self.bot._rewrites_in_progress:
            await interaction.followup.send("Options are already being generated for this draft.", ephemeral=True)
            return
        self.bot._rewrites_in_progress.add(self.article_id)
        try:
            draft = await self.bot.db_call(self.bot.store.get_draft, self.article_id)
            if draft is None or draft.status == "published":
                await interaction.followup.send("Only unpublished drafts can be rewritten.", ephemeral=True)
                return
            options = await self.bot.db_call(
                generate_draft_rewrites, draft, model=self.model_input.value.strip(),
                instructions=self.instructions_input.value.strip(),
            )
        except Exception as exc:  # Codex availability, invalid output, and timeout are recoverable.
            await interaction.followup.send(f"Could not generate draft options: {str(exc)[:500]}", ephemeral=True)
            return
        finally:
            self.bot._rewrites_in_progress.discard(self.article_id)

        for index, (title, body) in enumerate(zip(options.titles, options.bodies), start=1):
            await interaction.followup.send(
                f"Option {index}\nTitle {index}: {title}\n\nBody {index}:\n{body}",
                ephemeral=True, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none(),
            )
        view = RewriteChoiceView(self.bot, draft, options, interaction.user.id)
        view.message = await interaction.followup.send(
            view.preview(), view=view, ephemeral=True, wait=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RewriteChoiceView(discord.ui.View):
    def __init__(self, bot: NewsbotDiscordBot, draft: ArticleDraft, options: DraftRewriteOptions, user_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.draft = draft
        self.options = options
        self.user_id = user_id
        self.title_index: int | None = None
        self.body_index: int | None = None
        self.message: discord.WebhookMessage | None = None
        self.finished = False
        self.title_select = discord.ui.Select(
            placeholder="Choose a title", row=0,
            options=[discord.SelectOption(label=f"Title {i + 1}: {title}"[:100], value=str(i))
                     for i, title in enumerate(options.titles)],
        )
        self.body_select = discord.ui.Select(
            placeholder="Choose a body independently", row=1,
            options=[discord.SelectOption(label=f"Body {i + 1}", value=str(i), description=body.replace("\n", " ")[:100])
                     for i, body in enumerate(options.bodies)],
        )
        self.title_select.callback = self.choose_title
        self.body_select.callback = self.choose_body
        self.add_item(self.title_select)
        self.add_item(self.body_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.bot._require_reviewer(interaction):
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Generate your own options to edit this draft.", ephemeral=True)
            return False
        if self.finished:
            await interaction.response.send_message("This selection has already finished. Generate new options to continue.", ephemeral=True)
            return False
        return True

    def preview(self) -> str:
        title = self.options.titles[self.title_index] if self.title_index is not None else "(choose a title)"
        body = self.options.bodies[self.body_index] if self.body_index is not None else "(choose a body)"
        return f"Choose Title and Body separately, then Apply.\nModel: {self.options.model}\n\nTitle:\n{title}\n\nBody:\n{body}"

    async def choose_title(self, interaction: discord.Interaction) -> None:
        self.title_index = int(self.title_select.values[0])
        await self.update_preview(interaction)

    async def choose_body(self, interaction: discord.Interaction) -> None:
        self.body_index = int(self.body_select.values[0])
        await self.update_preview(interaction)

    async def update_preview(self, interaction: discord.Interaction) -> None:
        for select, index in ((self.title_select, self.title_index), (self.body_select, self.body_index)):
            for option in select.options:
                option.default = option.value == str(index)
        self.apply.disabled = self.title_index is None or self.body_index is None
        await interaction.response.edit_message(
            content=self.preview(), view=self, allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success, row=2, disabled=True)
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished or self.title_index is None or self.body_index is None:
            await interaction.response.send_message("Choose both a title and a body before applying.", ephemeral=True)
            return
        self.finished = True
        # A component update keeps the ephemeral choice message as the original
        # response. thinking=True would instead create a separate response.
        await interaction.response.defer(thinking=False)
        try:
            await self.bot.db_call(
                self.bot.store.update_draft_text,
                self.draft.id, self.options.titles[self.title_index], self.options.bodies[self.body_index],
                str(interaction.user.id), expected_draft=self.draft,
                reviewer_note=f"Rewrite model={self.options.model}; title={self.title_index + 1}; body={self.body_index + 1}",
            )
        except (KeyError, ValueError) as exc:
            content = "These options are out of date. Generate new options to continue."
            confirmation = str(exc)
        except Exception:
            self.finished = False
            await interaction.followup.send("Could not save the draft. Try Apply again.", ephemeral=True)
            return
        else:
            self.bot.schedule_refresh_review_message(self.draft.id)
            content = "Draft updated.\n\n" + self.preview()
            confirmation = "Selected title and body applied to the draft."
        # Saving has finished. A failed UI update must not leave an active view
        # or suggest retrying an edit that was already committed.
        self.stop()
        try:
            await interaction.edit_original_response(
                content=content, view=None, allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.warning("Could not update rewrite selection message for draft %s", self.draft.id, exc_info=True)
        await interaction.followup.send(confirmation, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.finished = True
        await interaction.response.edit_message(content="Rewrite cancelled. The draft was not changed.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        self.finished = True
        if self.message:
            try:
                await self.message.edit(content="Draft options expired. Use Rewrite Draft to generate new options.", view=None)
            except discord.HTTPException:
                pass
