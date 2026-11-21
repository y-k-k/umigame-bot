import os
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord
from discord.ext import commands
from dotenv import load_dotenv

import game_session as gs
import claude_client as cc
import quiz_workflow

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

PUZZLES_PER_PAGE = 25

_generating_channels: set[int] = set()
_cancel_requested: set[int] = set()


@bot.event
async def on_ready():
    print(f"Bot起動: {bot.user}")


def _make_discord_reporter(channel: discord.TextChannel):
    from review_evaluator import CHECK_LABELS

    async def reporter(event: quiz_workflow.WorkflowEvent) -> None:
        if event.type == "start":
            await channel.send("🎲 **自動作問モード開始**\n問題を生成しています...")
        elif event.type == "generated":
            await channel.send(
                f"📝 **作問完了**（{event.elapsed_secs}秒） "
                f"[{event.attempt}/{event.max_attempts}回目]"
            )
        elif event.type == "review_start":
            await channel.send("🔍 **校閲中...**")
        elif event.type == "check_result":
            label = CHECK_LABELS.get(event.check_name, event.check_name)
            icon = "✅" if event.check_passed else "❌"
            status = "PASS" if event.check_passed else "FAIL"
            await channel.send(f"{icon} {event.check_name} ({label}): {status}")
        elif event.type == "reviewed":
            if event.reviewed is None:
                await channel.send(f"⚠️ 校閲が解析できませんでした — {event.reason}")
            else:
                print(f"[review_quiz] {event.reviewed}")
                score = event.score if event.score is not None else "—"
                await channel.send(
                    f"📊 **校閲完了**（{event.elapsed_secs}秒） "
                    f"判定: **{event.verdict}** スコア: **{score}/10**"
                )
        elif event.type == "prepared":
            await channel.send(f"⚙️ **出題準備完了**（{event.elapsed_secs}秒）")
        elif event.type == "error":
            await channel.send(f"❌ {event.reason}")
        elif event.type == "exhausted":
            await channel.send(
                f"❌ {event.max_attempts}回試行しましたが、基準を満たす問題を生成できませんでした。"
            )
        elif event.type == "canceled":
            await channel.send("⛔ 自動作問をキャンセルしました。")
        # attempt / ready は黙って通過

    return reporter


GENERATE_MODEL_CHOICES: list[tuple[str, str, str]] = [
    ("Gemini 3.1 Pro Preview", "openrouter", "google/gemini-3.1-pro-preview"),
    ("Qwen 3.7 Max", "openrouter", "qwen/qwen3.7-max"),
    ("Claude Opus 4.8", "anthropic", "claude-opus-4-8"),
]


async def _do_auto_generate(
    channel: discord.TextChannel, *, backend: str, model: str,
) -> None:
    if channel.id in _generating_channels:
        await channel.send("⚠️ すでに自動作問が進行中です。`!中断` でキャンセルできます。")
        return

    _generating_channels.add(channel.id)
    _cancel_requested.discard(channel.id)

    try:
        result = await quiz_workflow.auto_generate(
            backend=backend,
            model=model,
            lenient_review=True,
            reporter=_make_discord_reporter(channel),
            is_canceled=lambda: channel.id in _cancel_requested,
        )
    finally:
        _generating_channels.discard(channel.id)
        _cancel_requested.discard(channel.id)

    if result is None:
        return

    session = gs.start_generated_session(
        channel.id,
        "ウミガメのスープ",
        result.reviewed["corrected_question"],
        result.reviewed["corrected_answer"],
        result.prepared["elements"],
        result.prepared.get("knowledge_base"),
    )

    embed = discord.Embed(
        title=f"🐢 {session.title}",
        description=session.question,
        color=discord.Color.teal(),
    )
    embed.set_footer(text="はい/いいえ/どちらともいえません/関係ありません で答えます。質問を自由に入力してください。")
    await channel.send(embed=embed)
    await channel.send(
        "**コマンド一覧**\n"
        "`!ヒント` — 次のヒントを1つ表示\n"
        "`!採点 <あなたの推理>` — 到達度＋わかっていることを詳しく確認\n"
        "`!答え合わせ` — 真相を表示してゲーム終了\n"
        "`!終了` — ゲームを中断（自動的に答え合わせを表示）"
    )


def _difficulty_stars(difficulty: int) -> str:
    filled = min(max(difficulty, 0), 5)
    return "★" * filled + "☆" * (5 - filled)


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
    if session.difficulty:
        embed.add_field(name="難易度", value=_difficulty_stars(session.difficulty), inline=False)
    embed.set_footer(text="はい/いいえ/どちらともいえません/関係ありません で答えます。質問を自由に入力してください。")
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


class GenerateModelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        for label, backend, model in GENERATE_MODEL_CHOICES:
            button = discord.ui.Button(
                label=label,
                custom_id=f"genmodel_{backend}_{model}",
                style=discord.ButtonStyle.primary,
            )
            button.callback = self._make_callback(backend, model, label)
            self.add_item(button)

    def _make_callback(self, backend: str, model: str, label: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
            await interaction.channel.send(f"🎯 **{label}** で自動作問を開始します")
            await _do_auto_generate(
                interaction.channel, backend=backend, model=model,
            )

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
    if puzzle_id == -1:
        if ctx.channel.id in _generating_channels:
            await ctx.send("⚠️ すでに自動作問が進行中です。`!中断` でキャンセルできます。")
            return
        view = GenerateModelSelectView()
        await ctx.send(
            "**🎲 自動作問モード** — 使用するモデルを選択してください",
            view=view,
        )
    else:
        await _do_start_game(ctx.channel, puzzle_id)


@bot.command(name="中断")
async def cancel_generate(ctx):
    if ctx.channel.id not in _generating_channels:
        await ctx.send("現在、自動作問は進行していません。")
        return
    _cancel_requested.add(ctx.channel.id)
    await ctx.send("⏳ 現在の作問が完了次第、自動作問を中断します...")


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

    if session.puzzle_id == -1:
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
        gs.end_session(ctx.channel.id)
        await ctx.send("ゲームを終了しました。")


@bot.command(name="遊び方")
async def how_to_play(ctx):
    await ctx.send(
        "**━━ ウミガメのスープ 遊び方 ━━**\n"
        "\n"
        "**📖 ゲームの概要**\n"
        "謎めいた場面が提示されます。「はい」「いいえ」「関係ない」で答えられる質問を繰り返し、真相を推理するゲームです。\n"
        "\n"
        "**🎮 ゲームの流れ**\n"
        "```\n"
        "1. !問題一覧      → 問題を選ぶ\n"
        "2. !開始 <番号>   → ゲーム開始。謎の場面が表示される\n"
        "3. チャットで質問 → 「〇〇ですか？」と送るとBotが答える\n"
        "                    ※ はい / いいえ / 関係ない で回答\n"
        "4. !ヒント        → 行き詰まったらヒントを使える\n"
        "5. !採点 <推理>   → 真相に近づいたと思ったら採点依頼\n"
        "                    到達度が100%になると自動で答え合わせ\n"
        "6. !答え合わせ    → 諦めたときや確認したいときは直接真相へ\n"
        "```\n"
        "\n"
        "**📋 コマンド一覧**\n"
        "`!問題一覧` — 問題一覧を表示\n"
        "`!問題一覧 <ページ>` — 指定ページの問題一覧を表示\n"
        "`!開始 <番号>` — ゲーム開始\n"
        "`!ヒント` — 依存解決済みの次のヒントを順に表示\n"
        "`!ヒント <番号>` — 指定番号のヒントを直接表示\n"
        "`!採点 <推理>` — 到達度＋わかっていることを詳しく確認\n"
        "`!答え合わせ` — 真相を表示してゲーム終了\n"
        "`!終了` — ゲームを中断\n"
        "`!遊び方` — この説明を表示"
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
    extra_knowledge = session.extra_knowledge or None
    async with message.channel.typing():
        reply = await asyncio.to_thread(
            cc.ask_question, session.question, session.answer, message.content, history, extra_knowledge
        )
    await message.reply(reply)
    gs.append_history(message.channel.id, message.content, reply)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError(".env に DISCORD_BOT_TOKEN が設定されていません")
    bot.run(token)
