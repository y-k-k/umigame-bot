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


@bot.event
async def on_ready():
    print(f"Bot起動: {bot.user}")


@bot.command(name="問題一覧")
async def list_puzzles(ctx):
    puzzles = gs.list_puzzles()
    lines = [f"**{p['id']}**: {p['title']}" for p in puzzles]
    await ctx.send("**── 問題一覧 ──**\n" + "\n".join(lines) + "\n\n`!開始 <番号>` で問題を選んでください")


@bot.command(name="開始")
async def start_game(ctx, puzzle_id: int):
    session = gs.start_session(ctx.channel.id, puzzle_id)
    if session is None:
        await ctx.send(f"問題番号 {puzzle_id} は存在しません。`!問題一覧` で確認してください。")
        return

    embed = discord.Embed(
        title=f"🐢 {session.title}",
        description=session.question,
        color=discord.Color.teal(),
    )
    embed.set_footer(text="はい/いいえ/関係ありません で答えます。質問を自由に入力してください。")
    await ctx.send(embed=embed)
    await ctx.send(
        "**コマンド一覧**\n"
        "`!ヒント` — 次のヒントを1つ表示\n"
        "`!進捗 <あなたの推理>` — 真相への到達度を確認\n"
        "`!採点 <あなたの推理>` — 到達度＋わかっていることを詳しく確認\n"
        "`!答え合わせ` — 真相を表示してゲーム終了\n"
        "`!終了` — ゲームを中断"
    )


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

    covered_hints, next_numbers = gs.get_score_details(ctx.channel.id, covered_ids)

    bar_filled = round(progress / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    lines = [f"**到達度: {progress}%** `{bar}`"]

    if progress == 100:
        lines.append("\n🎉 **真相に完全に到達しました！**\n`!答え合わせ` で模範解答を確認してみましょう。")
    else:
        if covered_hints:
            lines.append("\n**✅ わかっていること**")
            lines.extend(f"・{h}" for h in covered_hints)

        if next_numbers:
            nums = " / ".join(f"`!ヒント {n}`" for n in next_numbers)
            lines.append(f"\n**💡 次のステップ**: {nums}")

    await ctx.send("\n".join(lines))


@bot.command(name="進捗")
async def check_progress(ctx, *, theory: str):
    session = gs.get_session(ctx.channel.id)
    if session is None:
        await ctx.send("現在進行中のゲームがありません。`!開始 <番号>` で始めてください。")
        return

    async with ctx.typing():
        progress = await asyncio.to_thread(
            cc.check_progress, session.question, session.answer, theory, session.elements
        )

    bar_filled = round(progress / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    msg = f"**到達度: {progress}%** `{bar}`"
    if progress == 100:
        msg += "\n\n🎉 **真相に完全に到達しました！**\n`!答え合わせ` で模範解答を確認してみましょう。"
    await ctx.send(msg)


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
