import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

import game_session as gs
import claude_client as cc

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

PUZZLES_PER_PAGE = 25


@bot.event
async def on_ready():
    print(f"Bot起動: {bot.user}")


async def _do_start_game(channel: discord.TextChannel, puzzle_id: int) -> None:
    session = gs.start_session(channel.id, puzzle_id)
    if session is None:
        await channel.send(f"問題番号 {puzzle_id} は存在しません。`!問題一覧` で確認してください。")
        return

    embed = discord.Embed(
        title=f"🐢 {session.title}",
        description=session.question,
        color=discord.Color.teal(),
    )
    embed.set_footer(text="はい/いいえ/関係ありません で答えます。質問を自由に入力してください。")
    await channel.send(embed=embed)
    await channel.send(
        "**コマンド一覧**\n"
        "`!ヒント` — 次のヒントを1つ表示\n"
        "`!採点 <あなたの推理>` — 到達度＋わかっていることを詳しく確認\n"
        "`!答え合わせ` — 真相を表示してゲーム終了\n"
        "`!終了` — ゲームを中断"
    )


class PuzzleSelectView(discord.ui.View):
    def __init__(self, puzzles: list[dict]):
        super().__init__(timeout=None)
        for puzzle in puzzles:
            label = f"{puzzle['id']}: {puzzle['title']}"
            if len(label) > 80:
                label = label[:77] + "..."
            button = discord.ui.Button(
                label=label,
                custom_id=f"puzzle_{puzzle['id']}",
                style=discord.ButtonStyle.primary,
            )
            button.callback = self._make_callback(puzzle["id"])
            self.add_item(button)

    def _make_callback(self, puzzle_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            await interaction.channel.send(f"!開始 {puzzle_id}")
            await _do_start_game(interaction.channel, puzzle_id)

        return callback


@bot.command(name="問題一覧")
async def list_puzzles(ctx, page: int = 1):
    puzzles = gs.list_puzzles()
    total_pages = max(1, (len(puzzles) + PUZZLES_PER_PAGE - 1) // PUZZLES_PER_PAGE)

    if page < 1 or page > total_pages:
        await ctx.send(f"ページ {page} は存在しません（1〜{total_pages}ページ）。")
        return

    start = (page - 1) * PUZZLES_PER_PAGE
    page_puzzles = puzzles[start : start + PUZZLES_PER_PAGE]

    page_info = f"（{page}/{total_pages}ページ）" if total_pages > 1 else ""
    view = PuzzleSelectView(page_puzzles)
    await ctx.send(f"**── 問題一覧 {page_info}──**", view=view)


@bot.command(name="開始")
async def start_game(ctx, puzzle_id: int):
    await _do_start_game(ctx.channel, puzzle_id)


@bot.command(name="ヒント")
async def hint(ctx, number: int = None):
    session = gs.get_session(ctx.channel.id)
    if session is None:
        await ctx.send("現在進行中のゲームがありません。`!開始 <番号>` で始めてください。")
        return

    if number is not None:
        hint_text = gs.get_hint_by_number(ctx.channel.id, number)
        if hint_text is None:
            await ctx.send(f"ヒント{number}は存在しません。")
            return
        await ctx.send(f"💡 **ヒント{number}**: {hint_text}")
    else:
        result = gs.get_next_hint(ctx.channel.id)
        if result is None:
            if not session.elements:
                await ctx.send("この問題にはヒントがありません。")
            else:
                await ctx.send("これ以上ヒントはありません。`!答え合わせ` で真相を確認してみましょう。")
            return
        hint_text, hint_number, revealed, total = result
        revealed_numbers = [
            str(i + 1) for i, e in enumerate(session.elements)
            if e["id"] in session.revealed_ids
        ]
        footer = "次のヒントが欲しければ `!ヒント`"
        if len(revealed_numbers) > 1:
            footer += f"\nこれまでのヒントを確認したければ `!ヒント {'` / `!ヒント '.join(revealed_numbers)}`"
        await ctx.send(f"💡 **ヒント{hint_number}**: {hint_text}\n\n{footer}")


@bot.command(name="採点")
async def score(ctx, *, theory: str):
    session = gs.get_session(ctx.channel.id)
    if session is None:
        await ctx.send("現在進行中のゲームがありません。`!開始 <番号>` で始めてください。")
        return

    async with ctx.typing():
        progress, covered_ids = await asyncio.to_thread(
            cc.check_score, session.question, session.answer, theory, session.elements
        )

    bar_filled = round(progress / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    if progress == 100:
        await ctx.send(f"**到達度: {progress}%** `{bar}`\n\n🎉 **真相に完全に到達しました！**")
        async with ctx.typing():
            point = await asyncio.to_thread(
                cc.reveal_answer, session.question, session.answer, session.elements
            )
        question_count = session.question_count
        gs.end_session(ctx.channel.id)
        embed = discord.Embed(
            title="🐢 真相",
            description=session.answer,
            color=discord.Color.gold(),
        )
        embed.add_field(name="ポイント", value=point, inline=False)
        embed.set_footer(text=f"質問回数: {question_count}回")
        await ctx.send(embed=embed)
    else:
        covered_hints, next_numbers = gs.get_score_details(ctx.channel.id, covered_ids)
        lines = [f"**到達度: {progress}%** `{bar}`"]

        if covered_hints:
            lines.append("\n**✅ わかっていること**")
            lines.extend(f"・{h}" for h in covered_hints)

        if next_numbers:
            nums = " / ".join(f"`!ヒント {n}`" for n in next_numbers)
            lines.append(f"\n**💡 次のステップ**: {nums}")

        await ctx.send("\n".join(lines))


@bot.command(name="答え合わせ")
async def reveal(ctx):
    session = gs.get_session(ctx.channel.id)
    if session is None:
        await ctx.send("現在進行中のゲームがありません。")
        return

    async with ctx.typing():
        point = await asyncio.to_thread(
            cc.reveal_answer, session.question, session.answer, session.elements
        )

    question_count = session.question_count
    gs.end_session(ctx.channel.id)
    embed = discord.Embed(
        title="🐢 真相",
        description=session.answer,
        color=discord.Color.gold(),
    )
    embed.add_field(name="ポイント", value=point, inline=False)
    embed.set_footer(text=f"質問回数: {question_count}回")
    await ctx.send(embed=embed)


@bot.command(name="終了")
async def stop_game(ctx):
    session = gs.get_session(ctx.channel.id)
    if session is None:
        await ctx.send("現在進行中のゲームがありません。")
        return
    gs.end_session(ctx.channel.id)
    await ctx.send("ゲームを終了しました。")


@bot.command(name="help")
async def help_command(ctx):
    await ctx.send(
        "**── コマンド一覧 ──**\n"
        "`!問題一覧` — 問題一覧を表示\n"
        "`!問題一覧 <ページ>` — 指定ページの問題一覧を表示\n"
        "`!開始 <番号>` — ゲーム開始\n"
        "`!ヒント` — 依存解決済みの次のヒントを順に表示\n"
        "`!ヒント <番号>` — 指定番号のヒントを直接表示\n"
        "`!採点 <推理>` — 到達度＋わかっていることを詳しく確認\n"
        "`!答え合わせ` — 真相を表示してゲーム終了\n"
        "`!終了` — ゲームを中断\n"
        "`!help` — このコマンド一覧を表示"
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.content.startswith("!"):
        return

    if message.mentions or message.role_mentions or message.mention_everyone:
        return

    session = gs.get_session(message.channel.id)
    if session is None:
        return

    gs.increment_question_count(message.channel.id)
    history = gs.get_history(message.channel.id)
    async with message.channel.typing():
        reply = await asyncio.to_thread(
            cc.ask_question, session.question, session.answer, message.content, history
        )
    await message.reply(reply)
    gs.append_history(message.channel.id, message.content, reply)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError(".env に DISCORD_BOT_TOKEN が設定されていません")
    bot.run(token)
