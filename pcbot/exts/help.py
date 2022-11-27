import asyncio
from typing import Any, Mapping, Optional, Union

import discord
from discord.ext import commands
import snakecore

from ..bot import PygameCommunityBot

from .base import BaseCommandCog
from .text_command_manager import TextCommandManager

BotT = PygameCommunityBot


class EmbedHelpCommand(commands.HelpCommand):
    # Based on https://gist.github.com/Rapptz/31a346ed1eb545ddeb0d451d81a60b3b
    default_command_extras = {
        "invoke_on_message_edit": True,
        "response_deletion_with_reaction": True,
    }

    def __init__(self, **options: Any) -> None:
        if "command_attrs" in options:
            command_attrs = options["command_attrs"]

        else:
            options["command_attrs"] = command_attrs = {}

        if "extras" not in command_attrs:
            command_attrs["extras"] = {}

        command_attrs["extras"].update(self.default_command_extras)
        command_attrs["usage"] = "[name]"

        self.theme_color = discord.Color(int(options.get("theme_color", 0)))
        self.bot_help_message = options.get("bot_help_message", "")
        self.context: commands.Context[PygameCommunityBot]
        super().__init__(**options)

    def get_ending_note(self):
        return (
            f"Use {self.context.clean_prefix}{self.context.invoked_with} "
            "[name] for more info on a command or category."
        )

    def get_command_signature(
        self, command: commands.Command, escape_markdown: bool = True
    ):
        return (
            discord.utils.escape_markdown(
                f"{command.qualified_name} {command.signature}"
            )
            if escape_markdown
            else f"{command.qualified_name} {command.signature}"
        ).strip()

    async def send_bot_help(
        self, mapping: Mapping[Optional[commands.Cog], list[commands.Command]]
    ):
        start_embed_dict = {}
        start_embed_dict["title"] = "Help"
        start_embed_dict["color"] = int(self.theme_color)

        description = self.bot_help_message or self.context.bot.description
        if description:
            start_embed_dict["description"] = description

        embed_dict = start_embed_dict.copy()

        if self.context.guild:
            if mapping:
                embed_dict["fields"] = []
                embed_dict["fields"].append(
                    dict(
                        name="Categories: "
                        f"{len(mapping)-1 if None in mapping else len(mapping)}",
                        value="\u200b",
                    )
                )
            text_command_manager: TextCommandManager = self.context.bot.get_cog("text-command-manager")  # type: ignore

            for cog, cmds in mapping.items():
                name = "No Category" if cog is None else cog.qualified_name
                if text_command_manager:
                    filtered = [
                        cmd
                        for cmd in await self.filter_commands(cmds, sort=True)
                        if await text_command_manager.tcmd_can_run(self.context, cmd)
                    ]
                else:
                    filtered = await self.filter_commands(cmds, sort=True)
                if filtered:
                    value = "\u2002".join(
                        f"`{self.get_command_signature(c)}`" for c in filtered
                    )
                    if cog and cog.description:
                        value = f"{cog.description}\n\n**Commands**\n{value}"

                    embed_dict["fields"].append(
                        dict(name=name, value=value, inline=True)
                    )

            embed_dict["footer"] = dict(text=self.get_ending_note())

        await self.send_paginated_response_embeds(
            *(
                discord.Embed.from_dict(dct | start_embed_dict)
                for dct in snakecore.utils.embeds.split_embed_dict(embed_dict)
            ),
        )

    async def send_cog_help(self, cog: commands.Cog):
        if not self.context.guild:
            return

        start_embed_dict = {}
        start_embed_dict["title"] = f"`{cog.qualified_name}` Commands"
        start_embed_dict["color"] = int(self.theme_color)
        start_embed_dict["footer"] = dict(text=self.get_ending_note())

        embed_dict = start_embed_dict.copy()
        if cog.description:
            embed_dict["description"] = cog.description

        text_command_manager: TextCommandManager = self.context.bot.get_cog("text-command-manager")  # type: ignore

        if text_command_manager:
            filtered = [
                cmd
                for cmd in await self.filter_commands(cog.get_commands(), sort=True)
                if await text_command_manager.tcmd_can_run(self.context, cmd)
            ]
        else:
            filtered = await self.filter_commands(cog.get_commands(), sort=True)

        embed_dict["fields"] = []
        embed_dict["fields"].append(
            dict(name=f"Subcommands: {len(filtered)}", value="\u200b")
        )
        embed_dict["fields"].extend(
            (
                dict(
                    name=f"`{self.get_command_signature(command, escape_markdown=False)}`",
                    value=command.short_doc or "\u200b",
                    inline=False,
                )
                for command in filtered
            )
        )

        await self.send_paginated_response_embeds(
            *(
                discord.Embed.from_dict(
                    dct
                    | dict(
                        title=start_embed_dict["title"],
                        color=start_embed_dict["color"],
                        footer=start_embed_dict["footer"],
                    )
                )
                for dct in snakecore.utils.embeds.split_embed_dict(embed_dict)
            ),
        )

    async def send_group_help(self, group: commands.Group):
        if not self.context.guild:
            return

        start_embed_dict = {}
        start_embed_dict["title"] = f"Help for `{group.qualified_name}`"
        start_embed_dict["color"] = int(self.theme_color)
        if isinstance(group.cog, commands.Cog):
            start_embed_dict["author"] = dict(name=f"{group.cog.qualified_name}")

        embed_dict = start_embed_dict.copy()
        embed_dict["description"] = ""

        if (
            signature_str := self.get_command_signature(group, escape_markdown=False)
        ) != group.qualified_name:  # ignore empty signatures
            embed_dict["description"] = f"```\n{signature_str}```\n"

        if group.help:
            embed_dict["description"] += group.help

        text_command_manager: TextCommandManager = self.context.bot.get_cog("text-command-manager")  # type: ignore
        if text_command_manager:
            if not await text_command_manager.tcmd_can_run(self.context, group):
                return

        if isinstance(group, commands.Group):
            if text_command_manager:
                filtered = [
                    cmd
                    for cmd in await self.filter_commands(group.commands, sort=True)
                    if await text_command_manager.tcmd_can_run(self.context, cmd)
                ]
            else:
                filtered = await self.filter_commands(group.commands, sort=True)
            embed_dict["fields"] = []
            embed_dict["fields"].append(
                dict(name=f"Subcommands: {len(filtered)}", value="\u200b")
            )
            embed_dict["fields"].extend(
                (
                    dict(
                        name=f"`{self.get_command_signature(command, escape_markdown=False)}`",
                        value=command.short_doc or "\u200b",
                        inline=False,
                    )
                    for command in filtered
                )
            )

        embed_dict["footer"] = dict(text=self.get_ending_note())

        await self.send_paginated_response_embeds(
            *(
                discord.Embed.from_dict(dct | start_embed_dict)
                for dct in snakecore.utils.embeds.split_embed_dict(embed_dict)
            ),
        )

    # This makes it so it uses the function above
    # Less work for us to do since they're both similar.
    # If you want to make regular command help look different then override it
    send_command_help = send_group_help  # type: ignore

    async def send_error_message(self, error: str, /) -> None:
        return await self.send_paginated_response_embeds(
            discord.Embed(
                title="Something went wrong",
                description=error,
                color=int(self.theme_color),
            )
        )

    async def send_paginated_response_embeds(self, *embeds: discord.Embed):
        ctx = self.context

        # this shouldn't normally be false
        assert isinstance(ctx.author, discord.Member)

        paginator = None
        cog = self.cog
        if not isinstance(cog, BaseCommandCog):
            raise RuntimeError("A BaseCommandCog cog instance must be set")

        paginator = None

        if not embeds:
            return

        destination = self.get_destination()

        if (
            response_message := cog.cached_response_messages.get(ctx.message.id)
        ) is not None:
            try:
                if (
                    paginator_tuple := cog.cached_embed_paginators.get(
                        response_message.id
                    )
                ) is not None:
                    if paginator_tuple[0].is_running():
                        await paginator_tuple[0].stop()

                if len(embeds) == 1:
                    await response_message.edit(embed=embeds[0])
                    return

                paginator = snakecore.utils.pagination.EmbedPaginator(
                    (
                        response_message := await response_message.edit(
                            content="\u200b", embed=None
                        )
                    ),
                    *embeds,
                    member=ctx.author,
                    inactivity_timeout=60,
                    theme_color=int(self.theme_color),
                )
            except discord.NotFound:

                if len(embeds) == 1:
                    cog.cached_response_messages[
                        ctx.message.id
                    ] = await destination.send(embed=embeds[0])
                    return

                paginator = snakecore.utils.pagination.EmbedPaginator(
                    (response_message := await destination.send(content="\u200b")),
                    *embeds,
                    member=ctx.author,
                    inactivity_timeout=60,
                    theme_color=int(self.theme_color),
                )
        else:
            if len(embeds) == 1:
                cog.cached_response_messages[ctx.message.id] = await destination.send(
                    embed=embeds[0]
                )
                return

            paginator = snakecore.utils.pagination.EmbedPaginator(
                (response_message := await destination.send(content="\u200b")),
                *embeds,
                member=ctx.author,
                inactivity_timeout=60,
                theme_color=int(self.theme_color),
            )

        paginator_tuple = (
            paginator,
            asyncio.create_task(
                paginator.mainloop(client=ctx.bot),
                name=f"embed_paginator({response_message.jump_url})",
            ),
        )

        cog.cached_response_messages[ctx.message.id] = response_message
        cog.cached_embed_paginators[response_message.id] = paginator_tuple


class HelpCommandCog(BaseCommandCog, name="help-commands"):
    pass


@snakecore.commands.decorators.with_config_kwargs
async def setup(
    bot: BotT, bot_help_message: str = "", color: Union[int, discord.Color] = 0
):
    await bot.add_cog((help_command_cog := HelpCommandCog(bot)))
    embed_help_command = EmbedHelpCommand(
        bot_help_message=bot_help_message, theme_color=int(color)
    )
    bot.help_command = embed_help_command
    embed_help_command.cog = help_command_cog
