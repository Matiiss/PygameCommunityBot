"""This file is a part of the source code for PygameCommunityBot.
This project has been licensed under the MIT license.
Copyright (c) 2022-present pygame-community.
"""

from __future__ import annotations
import asyncio
import datetime
import itertools
import pickle
import re
import time

from typing import TYPE_CHECKING, Optional, TypedDict, Union

import discord
from discord.ext import commands, tasks
from packaging.version import Version
import snakecore
from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncConnection

from .migrations import REVISIONS, ROLLBACKS

from .constants import (
    DB_TABLE_PREFIX,
    HELP_FORUM_CHANNEL_IDS,
    FORUM_THREAD_TAG_LIMIT,
    INVALID_HELP_THREAD_TITLE_EMBEDS,
    INVALID_HELP_THREAD_TITLE_REGEX_PATTERNS,
    INVALID_HELP_THREAD_TITLE_SCANNING_ENABLED,
    INVALID_HELP_THREAD_TITLE_TYPES,
    THREAD_TITLE_TOO_SHORT_SLOWMODE_DELAY,
    THREAD_DELETION_MESSAGE_THRESHOLD,
)

from ... import __version__
from ..bases import ExtSpec, BaseExtCog
from ...bot import PygameCommunityBot

if TYPE_CHECKING:
    from typing_extensions import NotRequired  # type: ignore

BotT = PygameCommunityBot


class BadHelpThreadData(TypedDict):
    thread_id: int
    last_cautioned_ts: float
    caution_message_ids: set[int]


class InactiveHelpThreadData(TypedDict):
    thread_id: int
    last_active_ts: float
    alert_message_id: NotRequired[int]


class HelpForumsPreCog(BaseExtCog, name="helpforums-pre"):
    def __init__(
        self,
        bot: BotT,
        db_engine: AsyncEngine,
        revision_number: int,
        theme_color: Union[int, discord.Color] = 0,
    ) -> None:
        super().__init__(bot, theme_color=theme_color)
        self.bot: BotT
        self.db_engine = db_engine
        self.revision_number = revision_number

    async def cog_unload(self) -> None:
        self.inactive_help_thread_alert.stop()
        self.force_help_thread_archive_after_timeout.stop()
        self.delete_help_threads_without_starter_message.stop()

    @commands.Cog.listener()
    async def on_ready(self):
        for task_loop in (
            self.inactive_help_thread_alert,
            self.force_help_thread_archive_after_timeout,
            self.delete_help_threads_without_starter_message,
        ):
            if not task_loop.is_running():
                task_loop.start()

    @commands.Cog.listener()
    async def on_message_delete(self, msg: discord.Message):
        if (
            isinstance(msg.channel, discord.Thread)
            and msg.channel.parent_id in HELP_FORUM_CHANNEL_IDS.values()
            and msg.id == msg.channel.id  # OP deleted starter message
        ):
            await self.help_thread_deletion_checks(msg.channel)

    async def bad_help_thread_data_exists(self, thread_id: int) -> bool:
        conn: AsyncConnection
        async with self.db_engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text(
                            f"SELECT EXISTS(SELECT 1 FROM '{DB_TABLE_PREFIX}bad_help_thread_data' "
                            "WHERE thread_id == :thread_id LIMIT 1)"
                        ),
                        dict(thread_id=thread_id),
                    )
                ).scalar()
            )

    async def fetch_bad_help_thread_data(self, thread_id: int) -> BadHelpThreadData:
        conn: AsyncConnection
        async with self.db_engine.connect() as conn:
            result: Result = await conn.execute(
                text(
                    f"SELECT * FROM '{DB_TABLE_PREFIX}bad_help_thread_data' "
                    "WHERE thread_id == :thread_id"
                ),
                dict(thread_id=thread_id),
            )

            row = result.first()
            if not row:
                raise LookupError(
                    f"No bad help thread data found for thread with ID {thread_id}"
                )

            row_dict = row._asdict()  # type: ignore

            return BadHelpThreadData(
                thread_id=thread_id,
                last_cautioned_ts=row_dict["last_cautioned_ts"],
                caution_message_ids=pickle.loads(row_dict["caution_message_ids"]),
            )

    async def save_bad_help_thread_data(self, data: BadHelpThreadData) -> None:
        target_columns = (
            "thread_id",
            "last_cautioned_ts",
            "caution_message_ids",
        )
        target_update_set_columns = ", ".join((f"{k} = :{k}" for k in target_columns))

        conn: AsyncConnection
        async with self.db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO "
                    f"'{DB_TABLE_PREFIX}bad_help_thread_data' AS bad_help_thread_data "
                    f"({', '.join(target_columns)}) "
                    f"VALUES ({', '.join(':'+colname for colname in target_columns)}) "
                    f"ON CONFLICT DO UPDATE SET {target_update_set_columns} "
                    "WHERE bad_help_thread_data.thread_id == :thread_id"
                ),
                data
                | dict(caution_message_ids=pickle.dumps(data["caution_message_ids"])),
            )

    async def delete_bad_help_thread_data(self, thread_id: int) -> None:
        conn: AsyncConnection
        async with self.db_engine.connect() as conn:
            await conn.execute(
                text(
                    f"DELETE FROM '{DB_TABLE_PREFIX}bad_help_thread_data' "
                    "AS bad_help_thread_data "
                    "WHERE bad_help_thread_data.thread_id == :thread_id"
                ),
                dict(thread_id=thread_id),
            )

    async def inactive_help_thread_data_exists(self, thread_id: int) -> bool:
        conn: AsyncConnection
        async with self.db_engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text(
                            f"SELECT EXISTS(SELECT 1 FROM '{DB_TABLE_PREFIX}inactive_help_thread_data' "
                            "WHERE thread_id == :thread_id LIMIT 1)"
                        ),
                        dict(thread_id=thread_id),
                    )
                ).scalar()
            )

    async def fetch_inactive_help_thread_data(
        self, thread_id: int
    ) -> InactiveHelpThreadData:
        conn: AsyncConnection
        async with self.db_engine.connect() as conn:
            result: Result = await conn.execute(
                text(
                    f"SELECT * FROM '{DB_TABLE_PREFIX}inactive_help_thread_data' "
                    "WHERE thread_id == :thread_id"
                ),
                dict(thread_id=thread_id),
            )

            row = result.first()
            if not row:
                raise LookupError(
                    f"No inactive help thread data found for thread with ID {thread_id}"
                )

            row_dict = row._asdict()  # type: ignore

            output = InactiveHelpThreadData(
                thread_id=thread_id, last_active_ts=row_dict["last_active_ts"]
            )

            if "alert_message_id" in row_dict:
                output["alert_message_id"] = row_dict["alert_message_id"]

            return output

    async def save_inactive_help_thread_data(
        self, data: InactiveHelpThreadData
    ) -> None:
        target_columns = (
            "thread_id",
            "last_active_ts",
            "alert_message_id",
        )
        target_update_set_columns = ", ".join((f"{k} = :{k}" for k in target_columns))

        conn: AsyncConnection
        async with self.db_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO "
                    f"'{DB_TABLE_PREFIX}inactive_help_thread_data' AS inactive_help_thread_data "
                    f"({', '.join(target_columns)}) "
                    f"VALUES ({', '.join(':'+colname for colname in target_columns)}) "
                    f"ON CONFLICT DO UPDATE SET {target_update_set_columns} "
                    "WHERE inactive_help_thread_data.thread_id == :thread_id "
                ),
                data | dict(alert_message_id=data.get("alert_message_id", None)),
            )

    async def delete_inactive_help_thread_data(self, thread_id: int) -> None:
        conn: AsyncConnection
        async with self.db_engine.connect() as conn:
            await conn.execute(
                text(
                    f"DELETE FROM '{DB_TABLE_PREFIX}inactive_help_thread_data' "
                    "AS inactive_help_thread_data "
                    "WHERE inactive_help_thread_data.thread_id == :thread_id"
                ),
                dict(thread_id=thread_id),
            )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if thread.parent_id in HELP_FORUM_CHANNEL_IDS.values():
            caution_messages: list[discord.Message] = []
            issues_found = False
            thread_edits = {}
            try:
                await (
                    thread.starter_message
                    if thread.starter_message and thread.starter_message.id == thread.id
                    else (await thread.fetch_message(thread.id))
                ).pin()

                parent = (
                    thread.parent
                    or self.bot.get_channel(thread.parent_id)
                    or await self.bot.fetch_channel(thread.parent_id)
                )

                if (
                    len((applied_tags := thread.applied_tags)) < FORUM_THREAD_TAG_LIMIT
                    or len(applied_tags) == FORUM_THREAD_TAG_LIMIT
                    and any(
                        tag.name.lower().startswith("solved") for tag in applied_tags
                    )
                ):
                    new_tags = [
                        tag
                        for tag in applied_tags
                        if not tag.name.lower().startswith("solved")
                    ]

                    for tag in parent.available_tags:  # type: ignore
                        if tag.name.lower().startswith("unsolved"):
                            new_tags.insert(0, tag)  # mark help post as unsolved
                            break

                    thread_edits["applied_tags"] = new_tags

                if caution_types := self.get_help_forum_channel_thread_name_cautions(
                    thread
                ):
                    issues_found = True
                    caution_messages.extend(
                        await self.caution_about_help_forum_channel_thread_name(
                            thread, *caution_types
                        )
                    )
                    if "thread_title_too_short" in caution_types:
                        thread_edits.update(
                            dict(
                                slowmode_delay=THREAD_TITLE_TOO_SHORT_SLOWMODE_DELAY,
                                reason="Slowmode penalty for the title of this help post being too short.",
                            )
                        )
                if thread.parent_id == HELP_FORUM_CHANNEL_IDS["regulars"]:
                    if not self.validate_regulars_help_forum_channel_thread_tags(
                        thread
                    ):
                        issues_found = True
                        caution_messages.append(
                            await self.caution_about_regulars_help_forum_channel_thread_tags(
                                thread
                            )
                        )

                if issues_found and not await self.bad_help_thread_data_exists(
                    thread.id
                ):
                    await self.save_bad_help_thread_data(
                        {
                            "thread_id": thread.id,
                            "last_cautioned_ts": time.time(),
                            "caution_message_ids": set(
                                msg.id for msg in caution_messages
                            ),
                        }
                    )

                owner_id_suffix = f"│{thread.owner_id}"
                if not (
                    thread.name.endswith(owner_id_suffix)
                    or str(thread.owner_id) in thread.name
                ):
                    thread_edits["name"] = (
                        thread.name
                        if len(thread.name) < 72
                        else thread.name[:72] + "..."
                    ) + owner_id_suffix

                if thread_edits:
                    await thread.edit(**thread_edits)

            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        if after.parent_id in HELP_FORUM_CHANNEL_IDS.values():
            try:
                assert self.bot.user
                owner_id_suffix = f"│{after.owner_id}"
                if not (after.archived or after.locked):
                    thread_edits = {}
                    caution_messages: list[discord.Message] = []
                    bad_thread_name = False
                    bad_thread_tags = False

                    updater_id = None

                    async for action in after.guild.audit_logs(
                        limit=20, action=discord.AuditLogAction.thread_update
                    ):
                        if (target := action.target) and target.id == after.id:
                            if action.user:
                                updater_id = action.user.id
                                break

                    if before.name != after.name and updater_id != self.bot.user.id:  # type: ignore
                        if caution_types := self.get_help_forum_channel_thread_name_cautions(
                            after
                        ):
                            bad_thread_name = True
                            caution_messages.extend(
                                await self.caution_about_help_forum_channel_thread_name(
                                    after, *caution_types
                                )
                            )
                            if (
                                "thread_title_too_short" in caution_types
                                and after.slowmode_delay
                                < THREAD_TITLE_TOO_SHORT_SLOWMODE_DELAY
                            ):
                                thread_edits.update(
                                    dict(
                                        slowmode_delay=THREAD_TITLE_TOO_SHORT_SLOWMODE_DELAY,
                                        reason="Slowmode penalty for the title of this "
                                        "help post being too short.",
                                    )
                                )
                            elif (
                                "thread_title_too_short" not in caution_types
                                and after.slowmode_delay
                                == THREAD_TITLE_TOO_SHORT_SLOWMODE_DELAY
                            ):
                                thread_edits.update(
                                    dict(
                                        slowmode_delay=(
                                            after.parent
                                            or self.bot.get_channel(after.parent_id)
                                            or await self.bot.fetch_channel(
                                                after.parent_id
                                            )
                                        ).default_thread_slowmode_delay,  # type: ignore
                                        reason="This help post's title is not too short anymore.",
                                    )
                                )

                    elif (
                        before.applied_tags != after.applied_tags
                        and updater_id != self.bot.user.id
                    ):
                        if after.parent_id == HELP_FORUM_CHANNEL_IDS["regulars"]:
                            if not self.validate_regulars_help_forum_channel_thread_tags(
                                after
                            ):
                                bad_thread_tags = True
                                caution_messages.append(
                                    await self.caution_about_regulars_help_forum_channel_thread_tags(
                                        after
                                    )
                                )

                    if bad_thread_name or bad_thread_tags:
                        if not await self.bad_help_thread_data_exists(after.id):
                            await self.save_bad_help_thread_data(
                                {
                                    "thread_id": after.id,
                                    "last_cautioned_ts": time.time(),
                                    "caution_message_ids": set(
                                        msg.id for msg in caution_messages
                                    ),
                                }
                            )

                        bad_thread_data = await self.fetch_bad_help_thread_data(
                            after.id
                        )

                        await self.save_bad_help_thread_data(
                            {
                                "thread_id": after.id,
                                "last_cautioned_ts": time.time(),
                                "caution_message_ids": bad_thread_data.get(
                                    "caution_message_ids", set()
                                )
                                | set(msg.id for msg in caution_messages),
                            }
                        )
                    else:
                        if (
                            await self.bad_help_thread_data_exists(after.id)
                            and updater_id != self.bot.user.id
                        ) and not (
                            caution_types := self.get_help_forum_channel_thread_name_cautions(
                                after
                            )
                            or (
                                after.parent_id == HELP_FORUM_CHANNEL_IDS["regulars"]
                                and not self.validate_regulars_help_forum_channel_thread_tags(
                                    after
                                )
                            )
                        ):  # help thread doesn't have issues anymore
                            if (
                                after.slowmode_delay
                                == THREAD_TITLE_TOO_SHORT_SLOWMODE_DELAY
                            ):
                                thread_edits.update(
                                    dict(
                                        slowmode_delay=(
                                            after.parent
                                            or self.bot.get_channel(after.parent_id)
                                            or await self.bot.fetch_channel(
                                                after.parent_id
                                            )
                                        ).default_thread_slowmode_delay,  # type: ignore
                                        reason="This help post's title is not invalid anymore.",
                                    )
                                )

                            bad_thread_data = await self.fetch_bad_help_thread_data(
                                after.id
                            )

                            for msg_id in bad_thread_data["caution_message_ids"]:
                                try:
                                    await after.get_partial_message(msg_id).delete()
                                except discord.NotFound:
                                    pass

                            await self.delete_bad_help_thread_data(after.id)

                        solved_in_before = any(
                            tag.name.lower().startswith("solved")
                            for tag in before.applied_tags
                        )
                        solved_in_after = any(
                            tag.name.lower().startswith("solved")
                            for tag in after.applied_tags
                        )
                        if not solved_in_before and solved_in_after:
                            new_tags = [
                                tag
                                for tag in after.applied_tags
                                if not tag.name.lower().startswith("unsolved")
                            ]
                            await self.send_help_thread_solved_alert(after)
                            thread_edits.update(
                                dict(
                                    auto_archive_duration=60,
                                    reason="This help post was marked as solved.",
                                    applied_tags=new_tags,
                                )
                            )

                            if await self.inactive_help_thread_data_exists(after.id):
                                inactive_thread_data = (
                                    await self.fetch_inactive_help_thread_data(after.id)
                                )
                                try:
                                    if alert_message_id := inactive_thread_data.get(
                                        "alert_message_id", None
                                    ):
                                        try:
                                            await after.get_partial_message(
                                                alert_message_id
                                            ).delete()
                                        except discord.NotFound:
                                            pass
                                finally:
                                    await self.delete_inactive_help_thread_data(
                                        after.id
                                    )

                        elif solved_in_before and not solved_in_after:
                            parent = (
                                after.parent
                                or self.bot.get_channel(after.parent_id)
                                or await self.bot.fetch_channel(after.parent_id)
                            )  # type: ignore

                            new_tags = after.applied_tags
                            if len(new_tags) < FORUM_THREAD_TAG_LIMIT:
                                for tag in parent.available_tags:
                                    if tag.name.lower().startswith("unsolved"):
                                        new_tags.insert(
                                            0, tag
                                        )  # mark help post as unsolved
                                        break

                            slowmode_delay = discord.utils.MISSING
                            if (
                                after.slowmode_delay == 60
                            ):  # no custom slowmode override
                                slowmode_delay = parent.default_thread_slowmode_delay

                            thread_edits.update(
                                dict(
                                    auto_archive_duration=parent.default_auto_archive_duration,
                                    slowmode_delay=slowmode_delay,
                                    reason="This help post was unmarked as solved.",
                                    applied_tags=new_tags,
                                )
                            )

                    if thread_edits:
                        await asyncio.sleep(5)
                        await after.edit(
                            **thread_edits
                        )  # apply edits in a batch to save API calls

                elif after.archived and not after.locked:
                    if any(
                        tag.name.lower().startswith("solved")
                        for tag in after.applied_tags
                    ):
                        thread_edits = {}
                        parent: discord.ForumChannel = (
                            after.parent
                            or self.bot.get_channel(after.parent_id)
                            or await self.bot.fetch_channel(after.parent_id)
                        )  # type: ignore
                        if (
                            after.slowmode_delay == parent.default_thread_slowmode_delay
                        ):  # no custom slowmode override
                            thread_edits["slowmode_delay"] = 60

                        if not (
                            after.name.endswith(owner_id_suffix)
                            or str(after.owner_id) in after.name
                        ):  # wait for a few event loop iterations, before doing a second,
                            # check, to be sure that a bot edit hasn't already occured
                            thread_edits["name"] = (
                                after.name
                                if len(after.name) < 72
                                else after.name[:72] + "..."
                            ) + owner_id_suffix

                        if thread_edits:
                            await after.edit(archived=False)
                            await asyncio.sleep(5)
                            thread_edits["archived"] = True
                            await after.edit(**thread_edits)

            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        if await self.inactive_help_thread_data_exists(payload.thread_id):
            await self.delete_inactive_help_thread_data(payload.thread_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        try:
            if not snakecore.utils.is_emoji_equal(payload.emoji, "✅"):
                return

            channel = self.bot.get_channel(
                payload.channel_id
            ) or await self.bot.fetch_channel(payload.channel_id)
            if not isinstance(channel, discord.Thread):
                return

            msg = await channel.fetch_message(payload.message_id)
            if (
                channel.parent_id in HELP_FORUM_CHANNEL_IDS.values()
                and not channel.flags.pinned
            ):

                white_check_mark_reaction = discord.utils.find(
                    lambda r: isinstance(r.emoji, discord.PartialEmoji)
                    and snakecore.utils.is_emoji_equal(r.emoji, "✅"),
                    msg.reactions,
                )

                by_op = payload.user_id == channel.owner_id
                by_admin = (
                    payload.member and payload.member.guild_permissions.administrator
                )
                if not msg.pinned and (
                    by_op
                    or payload.member
                    and payload.member.guild_permissions.administrator
                ):
                    await msg.pin(
                        reason="The owner of this message's thread has marked it as helpful."
                        if by_op
                        else "An admin has marked this message as helpful."
                    )
                elif payload.user_id == msg.author.id and msg.id != channel.id:
                    await msg.remove_reaction("✅", msg.author)

                elif not msg.pinned and (
                    white_check_mark_reaction and white_check_mark_reaction.count >= 4
                ):
                    await msg.pin(
                        reason="Multiple members of this message's thread "
                        "have marked it as helpful."
                    )

                if (
                    msg.id == channel.id
                    and (by_op or by_admin)
                    and channel.applied_tags
                    and not any(
                        tag.name.lower() in ("solved", "invalid")
                        for tag in channel.applied_tags
                    )
                ) and len(
                    channel.applied_tags
                ) < FORUM_THREAD_TAG_LIMIT:  # help post should be marked as solved
                    for tag in (
                        channel.parent
                        or self.bot.get_channel(channel.parent_id)
                        or await self.bot.fetch_channel(channel.parent_id)
                    ).available_tags:  # type: ignore
                        if tag.name.lower().startswith("solved"):
                            new_tags = [
                                tg
                                for tg in channel.applied_tags
                                if tg.name.lower() != "unsolved"
                            ]
                            new_tags.append(tag)

                            await channel.edit(
                                reason="This help post was marked as solved by "
                                + ("the OP" if by_op else "an admin")
                                + " (via a ✅ reaction).",
                                applied_tags=new_tags,
                            )
                            break

        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        try:
            if not snakecore.utils.is_emoji_equal(payload.emoji, "✅"):
                return

            channel = self.bot.get_channel(
                payload.channel_id
            ) or await self.bot.fetch_channel(payload.channel_id)
            if not isinstance(channel, discord.Thread):
                return

            msg = await channel.fetch_message(payload.message_id)
            if (
                isinstance(msg.channel, discord.Thread)
                and msg.channel.parent_id in HELP_FORUM_CHANNEL_IDS.values()
            ):
                if not snakecore.utils.is_emoji_equal(payload.emoji, "✅"):
                    return

                white_check_mark_reaction = discord.utils.find(
                    lambda r: isinstance(r.emoji, discord.PartialEmoji)
                    and snakecore.utils.is_emoji_equal(r.emoji, "✅"),
                    msg.reactions,
                )

                by_op = payload.user_id == channel.owner_id
                by_admin = (
                    payload.member and payload.member.guild_permissions.administrator
                )

                if msg.pinned and (
                    (
                        not white_check_mark_reaction
                        or white_check_mark_reaction
                        and (
                            white_check_mark_reaction.count < 4
                            or white_check_mark_reaction.count < 2
                            and by_admin
                        )
                    )
                ):
                    await msg.unpin(
                        reason="Multiple members of this message's thread "
                        "have unmarked it as helpful."
                    )

                if (
                    msg.id == msg.channel.id
                    and (by_op or by_admin)
                    and msg.channel.applied_tags
                    and any(
                        tag.name.lower().startswith("solved")
                        for tag in msg.channel.applied_tags
                    )
                ):  # help post should be unmarked as solved
                    for tag in (
                        msg.channel.parent
                        or self.bot.get_channel(msg.channel.parent_id)
                        or await self.bot.fetch_channel(msg.channel.parent_id)
                    ).available_tags:  # type: ignore
                        if tag.name.lower().startswith("solved"):
                            await msg.channel.remove_tags(
                                tag,
                                reason="This help post was unmarked as solved by "
                                + ("the OP" if by_op else "an admin")
                                + " (via removing a ✅ reaction).",
                            )
                            break
        except discord.HTTPException:
            pass

    @tasks.loop(hours=1, reconnect=True)
    async def inactive_help_thread_alert(self):
        for forum_channel in [
            self.bot.get_channel(fid) or (await self.bot.fetch_channel(fid))
            for fid in HELP_FORUM_CHANNEL_IDS.values()
        ]:
            if not isinstance(forum_channel, discord.ForumChannel):
                return

            now_ts = time.time()
            for help_thread in itertools.chain(
                forum_channel.threads,
                [thr async for thr in forum_channel.archived_threads(limit=20)],
            ):
                try:
                    if not help_thread.created_at:
                        continue
                    last_active_ts = help_thread.created_at.timestamp()

                    if not (
                        help_thread.locked
                        or help_thread.flags.pinned
                        or any(
                            tag.name.lower().startswith("solved")
                            for tag in help_thread.applied_tags
                        )
                    ):
                        last_active_ts = (
                            await self.fetch_last_thread_activity_dt(help_thread)
                        ).timestamp()

                        if (now_ts - last_active_ts) > (3600 * 23 + 1800):  # 23h30m
                            if (
                                not await self.inactive_help_thread_data_exists(
                                    help_thread.id
                                )
                            ) or (
                                inactive_thread_data := await self.fetch_inactive_help_thread_data(
                                    help_thread.id
                                )
                            )[
                                "last_active_ts"
                            ] < last_active_ts:
                                if not (
                                    help_thread.archived
                                    and help_thread.archiver_id
                                    and (
                                        help_thread.archiver_id == help_thread.owner_id
                                        or forum_channel.permissions_for(
                                            help_thread.guild.get_member(
                                                help_thread.archiver_id
                                            )
                                            or await help_thread.guild.fetch_member(
                                                help_thread.archiver_id
                                            )
                                        ).manage_threads
                                    )  # allow alert supression by help thread owner/OP or forum channel moderator
                                ):
                                    alert_message = await help_thread.send(
                                        f"help-post-inactive(<@{help_thread.owner_id}>, **{help_thread.name}**)",
                                        embed=discord.Embed(
                                            title="Your help post has gone inactive... 💤",
                                            description=f"Your help post was last active **<t:{int(last_active_ts)}:R>** ."
                                            "\nHas your issue been solved? If so, mark it as **Solved** by "
                                            "doing one of these:\n\n"
                                            "  **• React on your starter message with ✅**.\n"
                                            "  **• Right-click on your post (click and hold on mobile), "
                                            "go to 'Edit Tags', select the `✅ Solved` tag and save your changes.**\n\n"
                                            "**Mark all messages you find helpful here with a ✅ reaction please** "
                                            "<:pg_robot:837389387024957440>\n\n"
                                            "*If your issue has't been solved, you may "
                                            "either wait for help or close this post.*",
                                            color=0x888888,
                                        ),
                                    )
                                    await self.save_inactive_help_thread_data(
                                        {
                                            "thread_id": help_thread.id,
                                            "last_active_ts": alert_message.created_at.timestamp(),
                                            "alert_message_id": alert_message.id,
                                        }
                                    )
                        elif (
                            await self.inactive_help_thread_data_exists(help_thread.id)
                            and (
                                alert_message_id := (
                                    inactive_thread_data := await self.fetch_inactive_help_thread_data(
                                        help_thread.id
                                    )
                                ).get("alert_message_id", None)
                            )
                        ) and (
                            (
                                partial_alert_message := help_thread.get_partial_message(
                                    alert_message_id
                                )
                            ).created_at.timestamp()
                            < last_active_ts  # someone messaged into the thread, prepare to delete alert message
                        ):
                            try:
                                last_message = await self.fetch_last_thread_message(
                                    help_thread
                                )
                                if last_message and not last_message.is_system():
                                    try:
                                        await partial_alert_message.delete()
                                    except discord.NotFound:
                                        pass
                                    finally:
                                        await self.save_inactive_help_thread_data(
                                            {
                                                "thread_id": help_thread.id,
                                                "last_active_ts": last_message.created_at.timestamp(),
                                                # erase alert_message_id by omitting it
                                            }
                                        )
                            except discord.NotFound:
                                pass

                except discord.HTTPException:
                    pass

    @tasks.loop(hours=1, reconnect=True)
    async def delete_help_threads_without_starter_message(self):
        for forum_channel in [
            self.bot.get_channel(fid) or (await self.bot.fetch_channel(fid))
            for fid in HELP_FORUM_CHANNEL_IDS.values()
        ]:
            for help_thread in itertools.chain(
                forum_channel.threads,  # type: ignore
                [thr async for thr in forum_channel.archived_threads(limit=20)],  # type: ignore
            ):
                try:
                    starter_message = (
                        help_thread.starter_message
                        or await help_thread.fetch_message(help_thread.id)
                    )
                except discord.NotFound:
                    pass
                else:
                    continue  # starter message still exists, skip
                snakecore.utils.hold_task(
                    asyncio.create_task(self.help_thread_deletion_checks(help_thread))
                )

    @tasks.loop(hours=1, reconnect=True)
    async def force_help_thread_archive_after_timeout(self):
        for forum_channel in [
            self.bot.get_channel(fid) or (await self.bot.fetch_channel(fid))
            for fid in HELP_FORUM_CHANNEL_IDS.values()
        ]:
            if not isinstance(forum_channel, discord.ForumChannel):
                return

            now_ts = time.time()
            for help_thread in forum_channel.threads:
                try:
                    if not help_thread.created_at:
                        continue

                    if not (
                        help_thread.archived
                        or help_thread.locked
                        or help_thread.flags.pinned
                    ):
                        last_active_ts = (
                            await self.fetch_last_thread_activity_dt(help_thread)
                        ).timestamp()
                        if (
                            now_ts - last_active_ts
                        ) / 60.0 > help_thread.auto_archive_duration:

                            thread_edits = {}
                            thread_edits["archived"] = True

                            if (
                                any(
                                    tag.name.lower().startswith("solved")
                                    for tag in help_thread.applied_tags
                                )
                                and help_thread.slowmode_delay
                                == forum_channel.default_thread_slowmode_delay
                            ):
                                # solved and no overridden slowmode
                                thread_edits["slowmode_delay"] = 60  # seconds

                            if not (
                                help_thread.name.endswith(
                                    owner_id_suffix := f"│{help_thread.owner_id}"
                                )
                                or str(help_thread.owner_id) in help_thread.name
                            ):  # wait for a few event loop iterations, before doing a second,
                                # check, to be sure that a bot edit hasn't already occured
                                thread_edits["archived"] = False
                                thread_edits["name"] = (
                                    help_thread.name
                                    if len(help_thread.name) < 72
                                    else help_thread.name[:72] + "..."
                                ) + owner_id_suffix

                            await help_thread.edit(
                                reason="This help thread has been closed "
                                "after exceeding its inactivity timeout.",
                                **thread_edits,
                            )
                except discord.HTTPException:
                    pass

    @staticmethod
    async def help_thread_deletion_checks(thread: discord.Thread):
        member_msg_count = 0
        try:
            async for thread_message in thread.history(
                limit=max(thread.message_count, 100)
            ):
                if (
                    not thread_message.author.bot
                    and thread_message.type == discord.MessageType.default
                ):
                    member_msg_count += 1
                    if member_msg_count >= THREAD_DELETION_MESSAGE_THRESHOLD:
                        break

            if member_msg_count < THREAD_DELETION_MESSAGE_THRESHOLD:
                await thread.send(
                    embed=discord.Embed(
                        title="Post scheduled for deletion",
                        description=(
                            "Someone deleted the starter message of this post.\n\n"
                            f"Since it contains less than "
                            f"{THREAD_DELETION_MESSAGE_THRESHOLD} messages sent by "
                            "server members, it will be deleted "
                            f"**<t:{int(time.time()+300)}:R>**."
                        ),
                        color=0x551111,
                    )
                )
                await asyncio.sleep(300)
                await thread.delete()
        except discord.HTTPException:
            pass

    @staticmethod
    def validate_help_forum_channel_thread_name(thread: discord.Thread) -> bool:
        return any(
            (
                INVALID_HELP_THREAD_TITLE_SCANNING_ENABLED[caution_type]
                and INVALID_HELP_THREAD_TITLE_REGEX_PATTERNS[caution_type].search(
                    " ".join(thread.name.replace(f"│{thread.owner_id}", "").split())
                )
                is not None
                for caution_type in INVALID_HELP_THREAD_TITLE_TYPES
            )
        )

    @staticmethod
    def get_help_forum_channel_thread_name_cautions(
        thread: discord.Thread,
    ) -> tuple[str, ...]:
        return tuple(
            (
                caution_type
                for caution_type in INVALID_HELP_THREAD_TITLE_TYPES
                if INVALID_HELP_THREAD_TITLE_SCANNING_ENABLED[caution_type]
                and INVALID_HELP_THREAD_TITLE_REGEX_PATTERNS[caution_type].search(
                    " ".join(thread.name.replace(f"│{thread.owner_id}", "").split())
                )  # normalize whitespace
                is not None
            )
        )

    @staticmethod
    async def caution_about_help_forum_channel_thread_name(
        thread: discord.Thread, *caution_types: str
    ) -> list[discord.Message]:
        caution_messages = []
        for caution_type in caution_types:
            caution_messages.append(
                await thread.send(
                    content=f"help-post-alert(<@{thread.owner_id}>, **{thread.name}**)",
                    embed=discord.Embed.from_dict(
                        INVALID_HELP_THREAD_TITLE_EMBEDS[caution_type]
                    ),
                )
            )

        return caution_messages

    @staticmethod
    def validate_regulars_help_forum_channel_thread_tags(
        thread: discord.Thread,
    ) -> bool:
        applied_tags = thread.applied_tags
        valid = True
        if applied_tags and not any(
            tag.name.lower().startswith(("solved", "invalid")) for tag in applied_tags
        ):
            issue_tags = tuple(
                tag for tag in applied_tags if tag.name.lower().startswith("issue")
            )
            aspect_tags = tuple(
                tag
                for tag in applied_tags
                if not tag.name.lower().startswith(("issue", "unsolved"))
            )
            if not len(issue_tags) or len(issue_tags) > 1 or not aspect_tags:
                valid = False

        return valid

    @staticmethod
    async def caution_about_regulars_help_forum_channel_thread_tags(
        thread: discord.Thread,
    ) -> discord.Message:
        return await thread.send(
            content=f"help-post-alert(<@{thread.owner_id}>, **{thread.name}**)",
            embed=discord.Embed(
                title="Your tag selection is incomplete",
                description=(
                    "Please pick exactly **1 issue tag** and **1-3 aspect tags**.\n\n"
                    "**Issue Tags** look like this: **(`issue: ...`)**.\n"
                    "**Aspect Tags** are all non-issue tags in lowercase, e.g. **(`💥 collisions`)**\n\n"
                    "**Example tag combination for reworking collisions:\n"
                    "(`🪛 issue: rework/optim.`) (`💥 collisions`)**.\n\n"
                    f"See the Post Guidelines of <#{thread.parent_id}> for more details.\n\n"
                    "To make changes to your post's tags, either right-click on "
                    "it (desktop/web) or click and hold on it (mobile), then click "
                    "on **'Edit Tags'** to see a tag selection menu. Remember to save "
                    "your changes after selecting the correct tag(s).\n\n"
                    "*Why is there no aspect tag for Python?*\n> "
                    f"We recommend using <#{HELP_FORUM_CHANNEL_IDS['python']}> for "
                    "Python-focused questions instead.\n\n"
                    "**Thank you for helping us maintain clean help forum channels** "
                    "<:pg_robot:837389387024957440>\n\n"
                    "This alert should disappear after you have made appropriate changes."
                ),
                color=0x36393F,
            ),
        )

    @staticmethod
    async def send_help_thread_solved_alert(thread: discord.Thread):
        await thread.send(
            content="help-post-solved",
            embed=discord.Embed(
                title="Post marked as solved",
                description=(
                    "This help post has been marked as solved.\n"
                    "It will now close with a 1 minute slowmode "
                    "after 1 hour of inactivity.\nFor the sake of the "
                    "OP, please avoid sending any further messages "
                    "that aren't essential additions to the currently "
                    "accepted answers.\n\n"
                    "**Mark all messages you find helpful here with a ✅ reaction "
                    "please** <:pg_robot:837389387024957440>\n\n"
                    "The slowmode and archive timeout will both be reverted "
                    "if this post is unmarked as solved."
                ),
                color=0x00AA00,
            ),
        )

    @staticmethod
    async def fetch_last_thread_activity_dt(
        thread: discord.Thread,
    ) -> datetime.datetime:
        """Get the last time this thread was active. This is usually
        the creation date of the most recent message.

        Parameters
        ----------
        thread : discord.Thread
            The thread.

        Returns
        -------
            datetime.datetime: The time.
        """
        last_active = thread.created_at
        last_message = thread.last_message
        if last_message is None:
            last_message_found = False
            if thread.last_message_id is not None:
                try:
                    last_message = await thread.fetch_message(thread.last_message_id)
                    last_message_found = True
                except discord.NotFound:
                    pass

            if not last_message_found:
                try:
                    last_messages = [msg async for msg in thread.history(limit=1)]
                    if last_messages:
                        last_message = last_messages[0]
                except discord.HTTPException:
                    pass

        if last_message is not None:
            last_active = last_message.created_at

        return last_active  # type: ignore

    @staticmethod
    async def fetch_last_thread_message(
        thread: discord.Thread,
    ) -> Optional[discord.Message]:
        """Get the last message sent in the given thread.

        Parameters
        ----------
        thread : discord.Thread
            The thread.

        Returns
        -------
        Optional[discord.Message]
            The message, if it exists.
        """
        last_message = thread.last_message
        if last_message is None:
            last_message_found = False
            if thread.last_message_id is not None:
                try:
                    last_message = await thread.fetch_message(thread.last_message_id)
                    last_message_found = True
                except discord.NotFound:
                    pass

            if not last_message_found:
                try:
                    last_messages = [msg async for msg in thread.history(limit=1)]
                    if last_messages:
                        last_message = last_messages[0]
                except discord.HTTPException:
                    pass

        return last_message


ext_spec = ExtSpec(
    __name__,
    REVISIONS,
    ROLLBACKS,
    True,  # Always set to True until CLI supports manual migration
    DB_TABLE_PREFIX,
)

# both will be needed for the CLI
migrate = ext_spec.migrate
rollback = ext_spec.rollback


@snakecore.commands.decorators.with_config_kwargs
async def setup(
    bot: BotT,
    color: Union[int, discord.Color] = 0
    # add more optional parameters as desired
):
    await ext_spec.prepare_setup(bot)
    extension_data = await bot.read_extension_data(ext_spec.name)
    await bot.add_cog(
        HelpForumsPreCog(
            bot,
            bot.get_database(),  # type: ignore
            extension_data["revision_number"],
            theme_color=int(color),
        )
    )
