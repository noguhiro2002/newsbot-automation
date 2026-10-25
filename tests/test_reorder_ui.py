import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.webhook.async_ import async_context

from newsbot.config import DiscordSettings
from newsbot.db import NewsbotStore
from newsbot.discord_bot import DigestPublishView, NewsbotDiscordBot
from newsbot.models import NewsItem
from newsbot.render import order_weekly_drafts, weekly_digest_section
from newsbot.reorder_ui import REOPEN_NOTICE, ReorderDigestView
from newsbot.review import build_review_custom_id


def add_draft(store, index, *, kind="news", title=None, batch="batch"):
    draft, _ = store.create_or_update_draft_from_item(
        "lab_automation", "weekly",
        NewsItem(title=title or f"記事 {index}", url=f"https://example.com/{batch}/{index}",
                 summary="Body", source="Example", importance_score=100 - index,
                 raw={"content_kind": kind}),
    )
    return store.set_draft_status(draft.id, "final_ready", "7", publish_type="weekly", scheduled_at=batch)


def snapshot(store, batch="batch"):
    return store.list_final_ready_weekly_batch("lab_automation", "weekly", batch)


def event(*, user_id=7, values=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild_id=1, data={"values": values or []},
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
        edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
    )


def audit_count(store):
    with store.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM review_actions WHERE action_type = 'reorder_digest'").fetchone()[0]


def missing_message():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10008, "message": "Unknown Message"})


class ReorderStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = NewsbotStore(Path(self.temp.name) / "newsbot.sqlite")
        self.store.init()

    def save(self, before, ordered_ids=None):
        return self.store.reorder_final_ready_weekly(
            "lab_automation", "weekly", ordered_ids or [d.id for d in reversed(before)], "7",
            before[0].scheduled_at, expected_drafts=before,
        )

    def test_snapshot_conflicts_leave_order_and_history_unchanged(self):
        for change in ("add", "cancel", "publish", "reorder", "section", "priority", "reschedule"):
            with self.subTest(change=change):
                for i in range(3):
                    add_draft(self.store, i, batch=change)
                before = snapshot(self.store, change)
                first = before[0]
                if change == "add":
                    add_draft(self.store, 3, batch=change)
                elif change == "cancel":
                    self.store.set_draft_status(first.id, "pending", "8")
                elif change == "publish":
                    self.store.mark_published(first.id, "10", "123", "weekly")
                elif change == "reorder":
                    self.save(before)
                elif change == "section":
                    with self.store.connect() as conn:
                        conn.execute("UPDATE article_drafts SET source_payload_json = ? WHERE id = ?",
                                     ('{"content_kind":"event_announcement"}', first.id))
                elif change == "priority":
                    self.store.adjust_draft_priority(first.id, "8", "priority_up")
                else:
                    self.store.set_draft_status(first.id, "final_ready", "8", scheduled_at="later")
                current = [d.to_dict() for d in snapshot(self.store, change)]
                count = audit_count(self.store)
                with self.assertRaisesRegex(ValueError, "Reopen"):
                    self.save(before)
                self.assertEqual([d.to_dict() for d in snapshot(self.store, change)], current)
                self.assertEqual(audit_count(self.store), count)

    def test_text_edits_are_preserved_and_other_batches_are_untouched(self):
        for i in range(3):
            add_draft(self.store, i)
        other = add_draft(self.store, 0, batch="other")
        before = snapshot(self.store)
        self.store.update_draft_text(before[0].id, "修正後のタイトル", "新しい本文", "8")
        after = self.save(before)
        self.assertEqual([d.id for d in after], [d.id for d in reversed(before)])
        self.assertEqual(after[-1].title_edited, "修正後のタイトル")
        self.assertEqual(after[-1].body_edited, "新しい本文")
        self.assertEqual(self.store.get_draft(other.id), other)
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM review_actions WHERE action_type = 'reorder_digest'").fetchall()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual([d["id"] for d in json.loads(row["after_json"])], [d.id for d in after])
            self.assertEqual(json.loads(row["before_json"])[0]["title_edited"], "修正後のタイトル")

    def test_audit_failure_rolls_back_every_order_update_and_audit_entry(self):
        for i in range(3):
            add_draft(self.store, i)
        before = snapshot(self.store)
        with self.store.connect() as conn:
            conn.execute("""
                CREATE TRIGGER reject_second_reorder BEFORE INSERT ON review_actions
                WHEN NEW.action_type = 'reorder_digest'
                  AND (SELECT COUNT(*) FROM review_actions WHERE action_type = 'reorder_digest') > 0
                BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(before)
        self.assertEqual(snapshot(self.store), before)
        self.assertEqual(audit_count(self.store), 0)

    def test_only_one_concurrent_save_can_use_the_same_snapshot(self):
        for i in range(3):
            add_draft(self.store, i)
        before = snapshot(self.store)
        def attempt():
            try:
                self.save(before)
                return "saved"
            except ValueError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertCountEqual(results, ["saved", "stale"])
        self.assertEqual(audit_count(self.store), 3)

    def test_incomplete_duplicate_and_foreign_ids_are_rejected(self):
        for i in range(3):
            add_draft(self.store, i)
        before = snapshot(self.store)
        ids = [d.id for d in before]
        for invalid in (ids[:2], [ids[0], ids[0], ids[2]], ids[:2] + ["missing"]):
            with self.assertRaises(ValueError):
                self.save(before, invalid)
        self.assertEqual(snapshot(self.store), before)
        self.assertEqual(audit_count(self.store), 0)


class ReorderViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = NewsbotStore(Path(self.temp.name) / "newsbot.sqlite")
        self.store.init()
        self.bot = NewsbotDiscordBot(
            store=self.store, settings=DiscordSettings("fake", "10", "20", frozenset({"7", "8"}), "1"),
        )
        self.bot.schedule_refresh_review_message = Mock()
        self.bot.get_channel = Mock(return_value=SimpleNamespace(id=20))
        self.bot.send_channel_message = AsyncMock(return_value=SimpleNamespace(id=123))
        self.views = []

    async def asyncTearDown(self):
        for view in self.views:
            view.stop()
        await self.bot.close()
        self.temp.cleanup()

    def view(self, drafts=None, source=None):
        view = ReorderDigestView(self.bot, snapshot(self.store) if drafts is None else drafts, 7, source)
        self.views.append(view)
        return view

    async def choose(self, view, article_id):
        await view.choose_article(event(values=[article_id]))

    async def test_counts_paging_and_discord_component_limits(self):
        for count in (0, 1, 10, 11, 35):
            with self.subTest(count=count):
                drafts = [add_draft(self.store, i, batch=str(count)) for i in range(count)]
                view = self.view(drafts)
                all_values = []
                for page in range(view.page_count):
                    self.assertEqual(view.page, page)
                    values = [o.value for o in view.article_select.options] if count else []
                    self.assertLessEqual(len(values), 10)
                    all_values.extend(values)
                    payload = view.preview()
                    self.assertLessEqual(len(payload["content"]), 2000)
                    self.assertLessEqual(len(payload["embed"]), 6000)
                    for field in payload["embed"].fields:
                        self.assertLessEqual(len(field.value), 1024)
                    components = view.to_components()
                    self.assertLessEqual(len(components), 5)
                    self.assertTrue(all(len(row["components"]) <= 5 for row in components))
                    if page < view.page_count - 1:
                        await view.next_page.callback(event())
                self.assertEqual(all_values, [d.id for d in drafts])
                self.assertTrue(view.next_page.disabled)
                self.assertTrue(view.save.disabled)
                if count <= 1:
                    self.assertTrue(all(b.disabled for b in (view.up, view.down, view.first, view.last)))
                if count == 0:
                    self.assertTrue(view.article_select.disabled)
                if count > 10:
                    await view.previous_page.callback(event())
                    self.assertEqual(view.page, view.page_count - 2)

    async def test_moving_across_page_keeps_selection_and_can_be_undone(self):
        for i in range(35):
            add_draft(self.store, i)
        before = snapshot(self.store)
        view = self.view()
        selected = view.ordered_ids[9]
        await self.choose(view, selected)
        await view.down.callback(event())
        self.assertEqual(view.page, 1)
        self.assertEqual(view.ordered_ids[10], selected)
        self.assertEqual([o.value for o in view.article_select.options if o.default], [selected])
        await view.up.callback(event())
        self.assertEqual(view.page, 0)
        self.assertTrue(view.save.disabled)
        await view.last.callback(event())
        self.assertEqual(view.page, 3)
        self.assertEqual(view.ordered_ids[-1], selected)
        self.assertTrue(view.down.disabled)
        self.assertTrue(view.last.disabled)
        await view.first.callback(event())
        self.assertEqual(view.page, 0)
        self.assertEqual(view.ordered_ids[0], selected)
        self.assertEqual(view.selected_id, selected)
        self.assertTrue(view.up.disabled)
        self.assertEqual(snapshot(self.store), before)
        self.assertEqual(audit_count(self.store), 0)

    async def test_news_and_events_cannot_cross_section_boundaries(self):
        for i, kind in enumerate(("event_announcement", "news", "event_occurrence", "news")):
            add_draft(self.store, i, kind=kind)
        view = self.view()
        self.assertEqual([weekly_digest_section(view.drafts[i]) for i in view.ordered_ids], ["News"] * 2 + ["Events"] * 2)
        await view.last.callback(event())
        self.assertEqual(view.ordered_ids.index(view.selected_id), 1)
        self.assertTrue(view.down.disabled)
        self.assertTrue(view.last.disabled)
        await view.down.callback(event())  # Stale/rapid taps cannot cross a boundary either.
        self.assertEqual(view.ordered_ids.index(view.selected_id), 1)
        await self.choose(view, view.ordered_ids[2])
        self.assertTrue(view.up.disabled)
        await view.first.callback(event())
        self.assertEqual(view.ordered_ids.index(view.selected_id), 2)
        await view.last.callback(event())
        self.assertEqual(view.ordered_ids.index(view.selected_id), 3)
        await view.up.callback(event())
        self.assertEqual(view.ordered_ids.index(view.selected_id), 2)

    async def test_long_and_duplicate_titles_use_ids_and_show_full_selected_title(self):
        title = "日本語の長いタイトル" * 150
        drafts = [add_draft(self.store, i, title=title) for i in range(11)]
        view = self.view()
        self.assertEqual(view.preview()["embed"].description, title)
        self.assertTrue(all(len(o.label) <= 100 for o in view.article_select.options))
        self.assertEqual(len({o.value for o in view.article_select.options}), 10)
        self.assertNotIn(title, view.preview()["embed"].fields[0].value)
        await self.choose(view, drafts[1].id)
        await view.first.callback(event())
        await view.save.callback(event())
        self.assertEqual(snapshot(self.store)[0].id, drafts[1].id)

    async def test_old_button_id_opens_private_editor_without_a_modal(self):
        drafts = [add_draft(self.store, i) for i in range(3)]
        custom_id = build_review_custom_id("reorder_digest", drafts[0].id)
        self.assertEqual(DigestPublishView(drafts[0].id).children[0].custom_id, custom_id)
        interaction = event()
        interaction.type = discord.InteractionType.component
        interaction.data = {"custom_id": custom_id}
        await self.bot.on_interaction(interaction)
        interaction.response.send_modal.assert_not_awaited()
        kwargs = interaction.followup.send.call_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertTrue(kwargs["wait"])
        view = kwargs["view"]
        self.views.append(view)
        self.assertIsInstance(view, ReorderDigestView)
        self.assertIs(view.source_message, interaction.message)
        self.assertIs(view.message, interaction.followup.send.return_value)
        self.assertEqual(audit_count(self.store), 0)

    async def test_many_long_titles_fit_shared_preview_after_save(self):
        for i in range(35):
            add_draft(self.store, i, title=f"記事{i} " + "長いタイトル" * 50,
                      kind="event_occurrence" if i >= 9 else "news")
        source = SimpleNamespace(edit=AsyncMock())
        view = self.view(source=source)
        # The first Event is on page 1; move it across pages within Events.
        selected = view.ordered_ids[9]
        await self.choose(view, selected)
        self.assertTrue(view.up.disabled)
        await view.last.callback(event())
        self.assertEqual(view.page, 3)
        self.assertEqual(view.selected_id, selected)
        await view.save.callback(event())
        content = source.edit.call_args.kwargs["content"]
        self.assertLessEqual(len(content), 2000)
        for i in range(1, 36):
            self.assertIn(f"\n{i}. ", content)
        self.assertEqual([d.id for d in snapshot(self.store)], view.ordered_ids)

    async def test_empty_batch_does_not_open_an_invalid_editor(self):
        draft = add_draft(self.store, 0)
        interaction = event()
        with patch.object(self.store, "list_final_ready_weekly_batch", return_value=[]):
            await self.bot._handle_reorder_digest(interaction, draft.id)
        self.assertNotIn("view", interaction.followup.send.call_args.kwargs)
        self.assertIn("ありません", interaction.followup.send.call_args.args[0])

    async def test_save_preview_and_publication_use_the_same_grouped_order(self):
        for i, kind in enumerate(("event_announcement", "news", "event_occurrence", "news")):
            add_draft(self.store, i, kind=kind)
        source = SimpleNamespace(edit=AsyncMock())
        view = self.view(source=source)
        await view.last.callback(event())
        await self.choose(view, view.ordered_ids[3])
        await view.first.callback(event())
        expected = view.ordered_ids.copy()
        await view.save.callback(event())
        self.assertEqual([d.id for d in snapshot(self.store)], expected)
        self.assertEqual([d.id for d in order_weekly_drafts(snapshot(self.store))], expected)
        content = source.edit.call_args.kwargs["content"]
        for i, article_id in enumerate(expected, 1):
            self.assertIn(f"{i}. {view.drafts[article_id].title_edited}", content)
        self.assertTrue(view.is_finished())
        self.assertEqual(audit_count(self.store), 4)
        await self.bot._publish_weekly_digest(event(), "lab_automation", "weekly", "batch")
        calls = self.bot.send_channel_message.call_args_list
        self.assertEqual(len(calls), 5)
        for call, article_id in zip(calls[1:], expected):
            self.assertIn(view.drafts[article_id].source_url, call.args[1])

    async def test_cancel_timeout_and_new_session_discard_unsaved_changes(self):
        for i in range(3):
            add_draft(self.store, i)
        before = snapshot(self.store)
        for action in ("cancel", "timeout", "restart"):
            with self.subTest(action=action):
                view = self.view()
                await view.last.callback(event())
                if action == "cancel":
                    await view.cancel.callback(event())
                elif action == "timeout":
                    view.message = SimpleNamespace(edit=AsyncMock())
                    await view.on_timeout()
                    view.message.edit.assert_awaited_once()
                else:
                    view.stop()
                    new_view = self.view()
                    self.assertEqual(new_view.ordered_ids, [d.id for d in before])
                self.assertEqual(snapshot(self.store), before)
                self.assertEqual(audit_count(self.store), 0)

    async def test_only_owner_in_allowed_guild_can_use_editor(self):
        add_draft(self.store, 0)
        view = self.view()
        self.assertTrue(await view.interaction_check(event()))
        self.assertFalse(await view.interaction_check(event(user_id=8)))
        self.assertFalse(await view.interaction_check(event(user_id=9)))
        wrong_guild = event()
        wrong_guild.guild_id = 2
        self.assertFalse(await view.interaction_check(wrong_guild))
        await view.on_timeout()
        self.assertFalse(await view.interaction_check(event()))

    async def test_two_open_editors_cannot_overwrite_each_other(self):
        for i in range(3):
            add_draft(self.store, i)
        first, second = self.view(), self.view()
        await first.last.callback(event())
        await second.down.callback(event())
        await first.save.callback(event())
        interaction = event()
        await second.save.callback(interaction)
        self.assertEqual(interaction.edit_original_response.call_args.kwargs["content"], REOPEN_NOTICE)
        self.assertEqual([d.id for d in snapshot(self.store)], first.ordered_ids)
        self.assertEqual(audit_count(self.store), 3)
        self.assertTrue(second.is_finished())

    async def test_repeated_save_clicks_only_commit_once(self):
        for i in range(3):
            add_draft(self.store, i)
        view = self.view()
        await view.last.callback(event())
        with patch.object(self.store, "reorder_final_ready_weekly", wraps=self.store.reorder_final_ready_weekly) as save:
            await asyncio.gather(view.save.callback(event()), view.save.callback(event()), view.cancel.callback(event()))
        save.assert_called_once()
        self.assertEqual(audit_count(self.store), 3)
        self.assertEqual([d.id for d in snapshot(self.store)], view.ordered_ids)

    async def test_save_waits_for_publication_and_then_refuses_stale_snapshot(self):
        for i in range(3):
            add_draft(self.store, i)
        before = snapshot(self.store)
        view = self.view()
        await view.last.callback(event())
        publish_started, continue_publish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            publish_started.set()
            await continue_publish.wait()
            return SimpleNamespace(id=123)
        self.bot.send_channel_message.side_effect = send
        publishing = asyncio.create_task(self.bot._publish_weekly_digest(event(), "lab_automation", "weekly", "batch"))
        await publish_started.wait()
        interaction = event()
        saving = asyncio.create_task(view.save.callback(interaction))
        continue_publish.set()
        await asyncio.gather(publishing, saving)
        self.assertEqual(audit_count(self.store), 0)
        self.assertEqual(interaction.edit_original_response.call_args.kwargs["content"], REOPEN_NOTICE)
        for call, draft in zip(self.bot.send_channel_message.call_args_list[1:], before):
            self.assertIn(draft.source_url, call.args[1])

    async def test_database_failure_allows_retry_without_losing_selection(self):
        for i in range(3):
            add_draft(self.store, i)
        before = snapshot(self.store)
        view = self.view()
        await view.last.callback(event())
        with patch.object(self.store, "reorder_final_ready_weekly", side_effect=sqlite3.OperationalError("locked")):
            with self.assertLogs("newsbot.reorder_ui", level="ERROR"):
                await view.save.callback(event())
        self.assertFalse(view.finished)
        self.assertEqual(snapshot(self.store), before)
        await view.save.callback(event())
        self.assertTrue(view.finished)
        self.assertEqual([d.id for d in snapshot(self.store)], view.ordered_ids)

    async def test_display_failure_after_save_still_confirms_success(self):
        for i in range(3):
            add_draft(self.store, i)
        source = SimpleNamespace(edit=AsyncMock(side_effect=missing_message()))
        view = self.view(source=source)
        await view.last.callback(event())
        interaction = event()
        interaction.edit_original_response.side_effect = missing_message()
        with self.assertLogs("newsbot.reorder_ui", level="WARNING"):
            await view.save.callback(interaction)
        confirmation = interaction.followup.send.call_args.args[0]
        self.assertIn("保存しました", confirmation)
        self.assertIn("再保存は不要", confirmation)
        self.assertTrue(view.finished)
        self.assertTrue(view.is_finished())
        self.assertEqual(audit_count(self.store), 3)
        self.assertFalse(await view.interaction_check(event()))

    def sdk_event(self, custom_id, *, values=None):
        message_data = {
            "id": "123", "channel_id": "10", "type": 0, "content": "Reorder",
            "flags": 64, "author": {"id": "99", "username": "bot", "discriminator": "0", "avatar": None},
        }
        self.bot._connection.user = discord.ClientUser(state=self.bot._connection, data=message_data["author"])
        interaction = discord.Interaction(data={
            "id": "456", "application_id": "99", "type": 3, "version": 1,
            "token": "fake-interaction-token", "attachment_size_limit": 1000000,
            "guild_id": "1", "channel": {"id": "10", "type": 0, "name": "review", "position": 0, "permission_overwrites": []},
            "member": {"user": {"id": "7", "username": "reviewer", "discriminator": "0", "avatar": None},
                       "roles": [], "joined_at": None, "flags": 0},
            "message": message_data,
            "data": {"custom_id": custom_id, "component_type": 3 if values else 2, "values": values or []},
        }, state=self.bot._connection)
        adapter = SimpleNamespace(
            create_interaction_response=AsyncMock(return_value={"interaction": {"id": "456"}}),
            edit_original_interaction_response=AsyncMock(return_value=message_data),
            edit_webhook_message=AsyncMock(return_value=message_data),
        )
        return interaction, adapter, message_data

    async def test_real_discord_sdk_uses_interaction_routes_for_all_private_updates(self):
        for i in range(11):
            add_draft(self.store, i)
        view = self.view(source=SimpleNamespace(edit=AsyncMock()))
        for action in ("select", "down", "last", "previous_page", "save", "cancel", "stale", "timeout"):
            with self.subTest(action=action):
                if view.is_finished():
                    view = self.view()
                    view.move_selected("last")
                item = view.article_select if action == "select" else (
                    view.save if action in {"stale", "timeout"} else getattr(view, action)
                )
                values = [view.ordered_ids[1]] if action == "select" else None
                interaction, adapter, message_data = self.sdk_event(item.custom_id, values=values)
                self.assertIs(type(interaction.message), discord.Message)
                token = async_context.set(adapter)
                try:
                    with patch.object(self.bot.http, "edit_message", new_callable=AsyncMock) as channel_edit:
                        channel_edit.side_effect = missing_message()
                        if action == "timeout":
                            view.message = interaction.followup._create_message(message_data, thread=discord.utils.MISSING)
                            self.assertIsInstance(view.message, discord.WebhookMessage)
                            await view.on_timeout()
                            adapter.edit_webhook_message.assert_awaited_once()
                        else:
                            if action == "stale":
                                self.store.set_draft_status(view.snapshot[0].id, "pending", "8")
                            await item.callback(interaction)
                            self.assertEqual(interaction.response.type, discord.InteractionResponseType.deferred_message_update)
                            edit = adapter.edit_original_interaction_response
                            edit.assert_awaited_once()
                            self.assertEqual(edit.call_args.args[:2], (99, "fake-interaction-token"))
                            if action in {"save", "cancel", "stale"}:
                                self.assertEqual(edit.call_args.kwargs["payload"]["components"], [])
                        channel_edit.assert_not_awaited()
                finally:
                    async_context.reset(token)


if __name__ == "__main__":
    unittest.main()
