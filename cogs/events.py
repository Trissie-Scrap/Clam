import discord
from discord.ext import commands, menus, tasks

from collections import Counter, defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import traceback
import json
import psutil
import typing
import asyncio
import asyncpg
import humanize
import pytz

from .utils.menus import MenuPages
from .utils import db, colors, human_time

utc = pytz.UTC


class EventNotFound(commands.BadArgument):
    pass


class EventSource(menus.ListPageSource):
    def __init__(self, data, ctx):
        super().__init__(data, per_page=10)
        self.ctx = ctx

    def format_page(self, menu, entries):
        offset = menu.current_page * self.per_page
        all_events = []
        for i, (todo_id, name, starts_at, participants) in enumerate(
            entries, start=offset
        ):
            formatted = starts_at.strftime("%b %d, %Y at %#I:%M %p UTC")
            joined = ":white_check_mark: " if self.ctx.author.id in participants else ""
            all_events.append(f"{joined}{name} `({todo_id})` - {formatted}")

        description = (
            f"Total: **{len(self.entries)}**\nKey: name `(id)` - date\n\n"
            + "\n".join(all_events)
        )

        em = discord.Embed(
            title="Events in this Server",
            description=description,
            color=colors.PRIMARY,
        )
        em.set_author(name=str(self.ctx.author), icon_url=self.ctx.author.avatar_url)
        em.set_footer(text=f"Page {menu.current_page + 1}/{self.get_max_pages()}")

        return em


class Events(db.Table):
    id = db.PrimaryKeyColumn()
    name = db.Column(db.String(length=64), index=True)
    description = db.Column(db.String(length=120))
    message_id = db.Column(db.Integer(big=True))
    channel_id = db.Column(db.Integer(big=True))
    guild_id = db.Column(db.Integer(big=True), index=True)
    owner_id = db.Column(db.Integer(big=True), index=True)
    participants = db.Column(db.Array(db.Integer(big=True)), index=True)
    notify_role = db.Column(db.Integer(big=True))
    created_at = db.Column(
        db.Datetime(), default="now() at time zone 'utc'", index=True
    )
    starts_at = db.Column(db.Datetime(), index=True)
    timezone = db.Column(db.String(length=5))


class Event:
    @classmethod
    def from_record(cls, record):
        self = cls()

        self.id = record["id"]
        self.name = record["name"]
        self.description = record["description"]
        self.message_id = record["message_id"]
        self.channel_id = record["channel_id"]
        self.guild_id = record["guild_id"]
        self.owner_id = record["owner_id"]
        self.participants = record["participants"]
        self.notify_role = record["notify_role"]
        self.created_at = record["created_at"]
        self.starts_at = record["starts_at"]
        self.timezone = record["timezone"]

        return self

    def format_time(self):
        return self.starts_at.strftime("%b %d, %Y at %#I:%M %p %z")


class EventConverter(commands.Converter):
    async def convert(self, ctx, argument):
        try:
            argument = int(argument)
        except ValueError:
            raise commands.BadArgument("Event must be int.")

        query = """SELECT *
                    FROM events
                    WHERE id=$1;
                """

        record = await ctx.db.fetchrow(query, argument)

        if not record:
            raise EventNotFound("Event was not found.")

        return Event.from_record(record)


class PartialEvent:
    def __init__(
        self,
        name,
        description,
        owner_id,
        starts_at,
        message_id,
        guild_id,
        channel_id,
        participants,
        notify_role,
        timezone="UTC",
    ):
        self.name = name
        self.description = description
        self.owner_id = owner_id
        self.starts_at = starts_at
        self.message_id = message_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.participants = participants
        self.notify_role = notify_role
        self.timezone = timezone


class Events(commands.Cog):
    """Easily create and manage events on Discord!"""

    def __init__(self, bot):
        self.bot = bot
        self.log = bot.log
        self.emoji = ":page_facing_up:"

        self._current_event = None
        self._event_ready = asyncio.Event()

        self._event_dispatch_task = self.bot.loop.create_task(
            self.event_dispatch_loop()
        )

    async def cog_command_error(self, ctx, error):
        if isinstance(error, EventNotFound):
            await ctx.send("Task was not found.")
            ctx.handled = True

    def cog_unload(self):
        self._event_dispatch_task.cancel()

    async def get_first_active_event(self, days=7):
        query = """SELECT *
                   FROM events
                   WHERE starts_at < (CURRENT_DATE + $1::interval)
                   ORDER BY starts_at
                   LIMIT 1;
                """

        record = await self.bot.pool.fetchrow(query, timedelta(days=days))
        return Event.from_record(record) if record else None

    async def get_active_events(self, days=40):
        event = await self.get_first_active_event(days)
        if event is not None:
            self._event_ready.set()
            return event

        self._event_ready.clear()
        self._current_event = None
        await self._event_ready.wait()
        return await self.get_first_active_event(days)

    async def end_event(self, event):
        guild = self.bot.get_guild(event.guild_id)

        query = "DELETE FROM events WHERE id=$1"

        result = await self.bot.pool.execute(query, event.id)

        if not guild:
            return

        channel = guild.get_channel(event.channel_id)

        if not channel:
            return

        message = await channel.fetch_message(event.message_id)

        em = message.embeds[0]
        em.color = discord.Color.orange()
        em.description = (event.description or "") + "\n\nSorry, the event has started."

        await message.edit(embed=em)

        if event.notify_role:
            notify_role = guild.get_role(event.notify_role)
            if notify_role:
                await channel.send(
                    f"{notify_role.mention}\nEvent `{event.name}` has started!"
                )
                await notify_role.delete()

        else:
            await channel.send(
                f"<@{event.owner_id}>\nEvent `{event.name}` has started!"
            )

    async def event_dispatch_loop(self):
        try:
            while not self.bot.is_closed():
                event = await self.get_active_events()
                self._current_event = event
                now = datetime.utcnow()

                if utc.localize(event.starts_at) >= utc.localize(now):
                    to_sleep = (
                        utc.localize(event.starts_at) - utc.localize(now)
                    ).total_seconds()
                    await asyncio.sleep(to_sleep)
                await self.end_event(event)
        except asyncio.CancelledError:
            raise
        except (OSError, discord.ConnectionClosed, asyncpg.PostgresConnectionError):
            self._event_dispatch_task.cancel()
            self._event_dispatch_task = self.bot.loop.create_task(
                self.event_dispatch_loop()
            )
        except:
            raise

    async def get_event(self, event_id):
        query = """SELECT * FROM events WHERE id=$1;"""
        async with self.bot.pool.acquire() as con:
            record = await con.fetchrow(query, event_id)
            if record is not None:
                return Event.from_record(record)
            return None

    async def delete_event(self, event):
        pass

    def create_event_embed(self, event):
        em = discord.Embed(
            title=event.name,
            description=(event.description or "")
            + (
                "\n\nReact with :white_check_mark: to RSVP! "
                "(You can click it again to leave.)"
                "\nReact with :pencil: to edit the event."
            ),
            color=discord.Color.green(),
            timestamp=event.starts_at,
        )
        guild = self.bot.get_guild(event.guild_id)
        participants = "\n".join(
            [guild.get_member(m).mention for m in event.participants]
        )
        em.add_field(name="Participants", value=participants or "\u200b")
        em.set_footer(text="Event starts")

        return em

    async def update_event_name_or_description(self, event, option, channel, check):
        await channel.send(f"What would you like to change the {option} to?")

        try:
            message = await self.bot.wait_for("message", timeout=60.0, check=check)

        except asyncio.TimeoutError:
            return await channel.send("You took too long. Aborting.")

        if len(message.content) > 60 and option == "name":
            return await channel.send("The name must be under 60 characters.")

        if len(message.content) > 120 and option == "description":
            return await channel.send("The description must be under 120 characters.")

        query = f"""UPDATE events
                    SET {option}=$1
                    WHERE id=$2
                """

        await self.bot.pool.execute(query, message.content, event.id)

        event_channel = self.bot.get_channel(event.channel_id)

        if not event_channel:
            await channel.send(
                "I couldn't find that event's channel. Was it deleted or hidden?"
            )

        else:

            try:
                event_message = await event_channel.fetch_message(event.message_id)

            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await channel.send(
                    "I couldn't find that event's message. Was it deleted?"
                )
            else:
                em = event_message.embeds[0]
                if option == "name":
                    em.title = message.content
                elif option == "description":
                    em.description.replace(event.description, message.content)
                await event_message.edit(embed=em)

        await channel.send(
            f":white_check_mark: Updated event name from `{event.name}` to `{message.content}`"
        )

    async def edit_event(self, event, member, channel, dm=True):
        if member.id != event.owner_id:
            return await channel.send(
                f"{member.mention}: Only the event creator can make edits.",
                delete_after=5.0,
            )

        options = {
            "timezone": "\N{CLOCK FACE ONE OCLOCK}",
            "name": "\N{LEFT SPEECH BUBBLE}",
            "description": "\N{PAGE FACING UP}",
            # "time": "\N{CLOCK FACE TEN OCLOCK}",
        }

        description = "\n".join(
            [f"{options[o]} {o.capitalize()}" for o in options.keys()]
        )

        em = discord.Embed(
            title=f":pencil: Edit Options",
            description=description,
            color=discord.Color.yellow(),
        )

        if dm:
            channel = member.dm_channel

        try:
            await channel.send(
                "Please respond with one of the options below.", embed=em
            )

        except discord.Forbidden:
            return await channel.send(
                f"{member.mention}: You must have DMs enabled to edit events.",
                delete_after=10.0,
            )

        def check(ms):
            return ms.author == member and ms.channel == channel

        try:
            message = await self.bot.wait_for("message", timeout=60.0, check=check)

        except asyncio.TimeoutError:
            return await channel.send("You took too long. Aborting.")

        option = message.content.lower()

        if option not in options.keys():
            return await channel.send("That isn't a vaild option.")

        if option in ["name", "description"]:
            await self.update_event_name_or_description(event, option, channel, check)

        elif option == "timezone":
            pass

    async def reaction_edit_event(self, payload):
        if not payload.guild_id:
            return

        guild = self.bot.get_guild(payload.guild_id)

        if not guild:
            return

        member = guild.get_member(payload.user_id)
        channel = guild.get_channel(payload.channel_id)

        if not channel:
            return

        if not member:
            return

        if member.bot:
            return

        query = """SELECT *
                   FROM events
                   WHERE message_id=$1 AND channel_id=$2;
                """

        record = await self.bot.pool.fetchrow(query, payload.message_id, channel.id)

        if not record:
            return

        event = Event.from_record(record)

        await self.edit_event(event, member, channel)

    async def member_join_event(self, payload):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)

        if not guild:
            return

        member = guild.get_member(payload.user_id)
        channel = guild.get_channel(payload.channel_id)

        if not channel:
            return

        if not member:
            return

        if member.bot:
            return

        query = """SELECT *
                   FROM events
                   WHERE message_id=$1 AND channel_id=$2;
                """

        record = await self.bot.pool.fetchrow(query, payload.message_id, channel.id)

        if not record:
            return

        event = Event.from_record(record)

        notify_role = guild.get_role(event.notify_role)

        if not notify_role:
            return

        if member.id in event.participants:
            event.participants.pop(event.participants.index(member.id))
            join = False
        else:
            event.participants.append(member.id)
            join = True

        message = await channel.fetch_message(payload.message_id)

        em = message.embeds[0]

        if not em.fields:
            return

        members_mentioned = "\n".join([f"<@{m}>" for m in event.participants])
        em.set_field_at(0, name="Participants", value=members_mentioned or "\u200b")

        query = """UPDATE events
                   SET participants=$1
                   WHERE id=$2;
                """

        await self.bot.pool.execute(query, event.participants, event.id)

        await message.edit(embed=em)

        em = discord.Embed(
            title=f"Successfully RSVP'd to {event.name}",
            description=f"Click :alarm_clock: to be notifed when the event starts.",
            color=discord.Color.red(),
        )

        if not join:
            return

        def check(reaction, user):
            return (
                reaction.emoji == "⏰"
                and user == member
                and reaction.message.channel == member.dm_channel
            )

        try:
            bot_msg = await member.send(embed=em)

            await bot_msg.add_reaction("\N{ALARM CLOCK}")

            try:
                # 24 hour timeout
                reaction, user = await self.bot.wait_for(
                    "reaction_add", check=check, timeout=86400.0
                )

                await member.add_roles(notify_role)

                await member.send("You will be notified when the event starts.")

            except asyncio.TimeoutError:
                pass

        except discord.Forbidden:
            await channel.send(
                f"{member.mention}: Please enable DMs in the future so I can send you event confirmation messages.",
                delete_after=10.0,
            )

    async def handle_reaction(self, payload):
        if str(payload.emoji) == "✅":
            await self.member_join_event(payload)

        elif str(payload.emoji) == "📝":
            await self.reaction_edit_event(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self.handle_reaction(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.handle_reaction(payload)

    @commands.group(invoke_without_command=True)
    async def event(self, ctx):
        """Create and manage events in Discord!

        Features:

        - Easily create events with a single command
        - Ability to edit the event
        - Set a reminder for the event
        """
        await ctx.send_help(ctx.command)

    @event.command(
        name="new", usage="[name]", aliases=["create", "add"],
    )
    @commands.bot_has_permissions(manage_roles=True, manage_messages=True)
    async def event_new(
        self,
        ctx,
        *,
        event: human_time.UserFriendlyTime(converter=commands.clean_content),
    ):
        """Create a new event

        Create a new event with a name and a date/time.
        The default timezone is UTC. You can change the timezone
        after you have created the event.

        Example command input:

        - Game night in two hours
        - 5d Birthday party
        - Party on 9/12/2020 at 2 PM
        """
        date = event.dt
        name = event.arg

        if not name:
            raise commands.BadArgument("You must specify a name for the event.")

        if len(name) > 64:
            return await ctx.send(
                "That name is too long. Must be 64 characters or less."
            )

        role_name = name[:20] if len(name) > 20 else name
        notify_role = await ctx.guild.create_role(
            name=f"EVENT: {role_name}", mentionable=True
        )

        embed = discord.Embed(
            description="Creating your event...", color=discord.Color.green()
        )
        msg = await ctx.send(embed=embed)

        query = """INSERT INTO events (name, description, owner_id, starts_at, message_id, guild_id, channel_id, participants, notify_role)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                   RETURNING id;
                """

        event_args = (
            name,
            None,
            ctx.author.id,
            date,
            msg.id,
            ctx.guild.id,
            ctx.channel.id,
            [ctx.author.id],
            notify_role.id,
        )

        result = await ctx.db.execute(query, *event_args)

        partial_event = PartialEvent(*event_args)

        delta = (date - datetime.utcnow()).total_seconds()

        if delta <= (86400 * 7):  # 7 days
            self._event_ready.set()

        if self._current_event and date < self._current_event.starts_at:
            self._event_dispatch_task.cancel()
            self._event_dispatch_task = self.bot.loop.create_task(
                self.event_dispatch_loop()
            )

        embed = self.create_event_embed(partial_event)
        await msg.edit(embed=embed)
        await msg.add_reaction("\N{WHITE HEAVY CHECK MARK}")
        await msg.add_reaction("\N{MEMO}")

        em = discord.Embed(
            title=f"Successfully created and RSVP'd to {name}",
            description=f"Click :alarm_clock: to be notifed when the event starts.",
            color=discord.Color.red(),
        )

        def check(reaction, user):
            return (
                reaction.emoji == "⏰"
                and user == ctx.author
                and reaction.message.channel == ctx.author.dm_channel
            )

        try:
            bot_msg = await ctx.author.send(embed=em)

            await bot_msg.add_reaction("\N{ALARM CLOCK}")

            try:
                # 24 hour timeout
                reaction, user = await self.bot.wait_for(
                    "reaction_add", check=check, timeout=86400.0
                )

                await ctx.author.add_roles(notify_role)

                await ctx.author.send("You will be notified when the event starts.")

            except asyncio.TimeoutError:
                pass

        except discord.Forbidden:
            await ctx.send(
                f"{ctx.author.mention}: Please enable DMs in the future so I can send you event confirmation messages.",
                delete_after=10.0,
            )

    @event.command(name="join", description="Join an event", usage="[name or id]")
    async def event_join(self, ctx, event: EventConverter):
        pass

    @event.command(name="leave", description="Leave an event", usage="[name or id]")
    async def event_leave(self, ctx, event: EventConverter):
        pass

    @event.command(
        name="edit", description="Edit an event", usage="[name or id]",
    )
    async def event_edit(self, ctx, *, task):
        try:
            task = int(task)
            sql = """UPDATE todos
                     SET completed_at=NOW() AT TIME ZONE 'UTC'
                     WHERE author_id=$1 AND id=$2;
                  """
        except ValueError:
            task = task
            sql = """UPDATE todos
                     SET completed_at=NOW() AT TIME ZONE 'UTC'
                     WHERE author_id=$1 AND name=$2;
                  """

        result = await ctx.db.execute(sql, ctx.author.id, task)
        if result.split(" ")[1] == "0":
            return await ctx.send("Task was not found.")

        await ctx.send(":ballot_box_with_check: Task marked as done")

    @event.command(
        name="delete",
        description="Delete an event",
        usage="[name or id]",
        aliases=["remove", "cancel"],
    )
    async def event_delete(self, ctx, *, event: EventConverter):
        query = "DELETE FROM events WHERE id=$1 AND owner_id=$2 AND guild_id=$3;"

        if ctx.author.id != event.owner_id:
            return await ctx.send("You do not own this event.")

        result = await ctx.db.execute(query, event.id, ctx.author.id, ctx.guild.id)
        if result.split(" ")[1] == "0":
            return await ctx.send(
                f"An event called `{event}` with you as the owner was not found."
            )

        channel = self.bot.get_channel(event.channel_id)
        try:
            message = await channel.fetch_message(event.message_id)

            em = message.embeds[0]
            em.description = (
                event.description or ""
            ) + "Sorry, this event has been cancelled."
            em.color = discord.Color.orange()

            await message.edit(embed=em)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        if event.notify_role:
            role = ctx.guild.get_role(event.notify_role)
            if role:
                await role.delete()

        await ctx.send(":wastebasket: Event cancelled and deleted.")

    @event.command(
        name="info",
        description="View info about an event",
        usage="[name or id]",
        aliases=["information"],
    )
    async def event_info(self, ctx, *, task: EventConverter):
        todo_id, name, created_at, completed_at = task

        if completed_at:
            description = f":ballot_box_with_check: ~~{name}~~ `({todo_id})`"
            description += f"\nCreated {humanize.naturaldate(created_at)}."
            description += f"\nCompleted {humanize.naturaldate(completed_at)}."
        else:
            description = f":black_large_square: {name} `({todo_id})`"
            description += f"\nCreated {humanize.naturaldate(created_at)}."

        em = discord.Embed(
            title="Event Info",
            description=description,
            color=colors.PRIMARY,
            timestamp=created_at,
        )

        em.set_author(name=str(ctx.author), icon_url=ctx.author.avatar_url)
        em.set_footer(text="Event starts")

        await ctx.send(embed=em)

    @event.command(
        name="list", description="List all upcoming events", aliases=["upcoming"],
    )
    async def event_list(self, ctx):
        query = """SELECT id, name, starts_at, participants
                   FROM events
                   WHERE guild_id=$1
                   ORDER BY starts_at DESC
                """

        records = await ctx.db.fetch(query, ctx.guild.id)

        if not records:
            return await ctx.send("There are no events in this server.")

        pages = MenuPages(source=EventSource(records, ctx), clear_reactions_after=True,)
        await pages.start(ctx)

    @event.command(name="all", description="View all events for this guild")
    async def event_all(self, ctx):
        query = """SELECT id, name, completed_at
                   FROM todos
                   WHERE author_id=$1
                   ORDER BY created_at DESC
                """

        records = await ctx.db.fetch(query, ctx.author.id)

        if not records:
            return await ctx.send("You have no tasks.")

        pages = MenuPages(
            source=EventSource(records, ctx, "all"), clear_reactions_after=True,
        )
        await pages.start(ctx)


def setup(bot):
    bot.add_cog(Events(bot))
