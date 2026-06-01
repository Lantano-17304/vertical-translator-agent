"""LLM-as-a-judge regression tests for the domain translation Agent.

Run from the repository root:
    python eval/llm_judge.py

The script calls the existing LangChain Agent, then asks an LLM judge to score
whether the translation preserves the expected domain-term meaning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

load_dotenv(ROOT / ".env")
load_dotenv()

from app.agent.core import agent_executor  # type: ignore[import-not-found]  # noqa: E402


@dataclass(frozen=True)
class EvalCase:
    id: str
    source: str
    expected_meaning: str
    required_terms: tuple[str, ...]


CASES = [
    EvalCase(
        id="debuff_basic",
        source="このキャラのデバフが強すぎる。",
        expected_meaning="デバフ是游戏中的负面状态/减益效果。",
        required_terms=("负面", "减益"),
    ),
    EvalCase(
        id="egui_slang",
        source="このボスの火力、マジでエグい。",
        expected_meaning="エグい在游戏/ACG语境中表示离谱、变态、强得夸张。",
        required_terms=("离谱", "变态", "夸张"),
    ),
    EvalCase(
        id="buff_basic",
        source="開幕で攻撃バフを盛ってから殴ろう。",
        expected_meaning="バフ是增益状态，例如攻击力提升。",
        required_terms=("增益", "提升"),
    ),
    EvalCase(
        id="one_pan",
        source="この装備なら雑魚敵はワンパンできる。",
        expected_meaning="ワンパン是一击击败/一击必杀。",
        required_terms=("一击", "秒杀", "一击必杀"),
    ),
    EvalCase(
        id="mixed_terms",
        source="バフを剥がされてデバフまで入るのはきつい。",
        expected_meaning="同时涉及增益被解除和负面状态被施加。",
        required_terms=("增益", "负面", "减益"),
    ),
    EvalCase(
        id="casual_jp",
        source="このガチャ、排出率が渋すぎて泣いた。",
        expected_meaning="渋い/渋すぎる在抽卡语境里表示概率低、出货抠门。",
        required_terms=("概率", "出货", "抽卡"),
    ),
    EvalCase(
        id="meta_game",
        source="今の環境だと、この編成がTier1だと思う。",
        expected_meaning="環境/Tier1 指当前版本强势、第一梯队阵容。",
        required_terms=("版本", "环境", "第一梯队", "强势"),
    ),
    EvalCase(
        id="cooldown",
        source="スキルのCTが長いから、使うタイミングが大事。",
        expected_meaning="CT 是技能冷却时间，强调释放时机。",
        required_terms=("冷却", "时机"),
    ),
    EvalCase(
        id="nerf",
        source="次のアプデでこの武器がナーフされるらしい。",
        expected_meaning="ナーフ是削弱，アプデ是更新。",
        required_terms=("削弱", "更新"),
    ),
    EvalCase(
        id="farming",
        source="素材集めはこのステージを周回するのが一番効率いい。",
        expected_meaning="周回是反复刷关/刷本以获取素材。",
        required_terms=("刷", "反复", "素材"),
    ),
    EvalCase(
        id="pity_system",
        source="あと20連で天井だから、このまま引き切るべき？",
        expected_meaning="天井是抽卡保底，あと20連表示还差20抽到保底。",
        required_terms=("保底", "抽"),
    ),
    EvalCase(
        id="off_rate_pull",
        source="限定狙いだったのに、またすり抜けた。",
        expected_meaning="すり抜け是抽到非目标/非UP角色，也就是歪卡。",
        required_terms=("歪", "目标", "限定"),
    ),
    EvalCase(
        id="dupe_upgrade",
        source="このキャラは2凸から使い勝手がかなり良くなる。",
        expected_meaning="凸表示突破/命座/星级提升，2凸指第二阶段突破效果。",
        required_terms=("突破", "命座", "2"),
    ),
    EvalCase(
        id="meta_must_pull",
        source="このサポーターは今の環境だと人権キャラだね。",
        expected_meaning="人権キャラ是高泛用高价值的必抽/人权角色，環境是当前版本环境。",
        required_terms=("人权", "必抽", "版本", "环境"),
    ),
    EvalCase(
        id="broken_character",
        source="新キャラ、火力も耐久も高くて完全にぶっ壊れ。",
        expected_meaning="ぶっ壊れ/壊れ性能表示角色强度超模，火力是输出，耐久是生存。",
        required_terms=("超模", "输出", "生存"),
    ),
    EvalCase(
        id="team_comp",
        source="初心者ならこの編成のほうが立ち回りやすい。",
        expected_meaning="編成是阵容/配队，立ち回り是操作思路/打法。",
        required_terms=("阵容", "配队", "操作", "打法"),
    ),
    EvalCase(
        id="boss_gimmick",
        source="この高難度はギミックを理解しないと初見殺しされる。",
        expected_meaning="高難度是高难本，ギミック是机制，初見殺し是初见杀。",
        required_terms=("高难", "机制", "初见杀"),
    ),
    EvalCase(
        id="role_party",
        source="タンクがヘイトを取って、ヒーラーが回復を回す感じ。",
        expected_meaning="タンク是坦克/承伤位，ヘイト是仇恨，ヒーラー是治疗/奶妈。",
        required_terms=("坦克", "仇恨", "治疗", "奶妈"),
    ),
    EvalCase(
        id="stun_lock",
        source="スタンが入れば、そのままハメて倒せる。",
        expected_meaning="スタン是眩晕/控制，ハメ是连控/卡死/压制。",
        required_terms=("眩晕", "控制", "连控", "压制"),
    ),
    EvalCase(
        id="reroll",
        source="リセマラで神引きしたから、このアカウントで始める。",
        expected_meaning="リセマラ是刷初始，神引き是欧皇抽卡/神抽。",
        required_terms=("刷初始", "欧皇", "神抽"),
    ),
]


def build_judge() -> ChatOpenAI:
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    model = "deepseek-chat" if base_url and "deepseek" in base_url.lower() else "gpt-3.5-turbo"
    return ChatOpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=base_url,
        model=model,
        temperature=0,
    )


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Judge did not return JSON: {text}")
    return json.loads(text[start : end + 1])


async def translate(source: str) -> str:
    result = await agent_executor.ainvoke({"input": source})
    return str(result.get("output", "")).strip()


async def judge_case(judge: ChatOpenAI, case: EvalCase, translation: str) -> dict:
    prompt = f"""
你是一个严格的翻译质量裁判。请判断候选译文是否准确保留了垂直游戏/ACG领域术语含义。

只返回 JSON，不要输出 Markdown。

评分规则：
- pass: true 表示译文基本正确覆盖 expected_meaning。
- score: 0 到 5 的整数；5=完整准确，4=基本准确，3=部分准确，2=明显遗漏，1=严重错误，0=无关。
- reason: 用一句中文说明理由。

source: {case.source}
expected_meaning: {case.expected_meaning}
required_terms_hint: {", ".join(case.required_terms)}
candidate_translation: {translation}

返回格式：
{{"pass": true, "score": 5, "reason": "..." }}
""".strip()
    message = await judge.ainvoke(prompt)
    data = extract_json(str(message.content))
    return {
        "pass": bool(data.get("pass")),
        "score": int(data.get("score", 0)),
        "reason": str(data.get("reason", "")),
    }


async def run(limit: int | None = None) -> int:
    judge = build_judge()
    cases = CASES[:limit] if limit else CASES
    rows = []

    for idx, case in enumerate(cases, start=1):
        print(f"[{idx}/{len(cases)}] Translating {case.id} ...")
        translation = await translate(case.source)
        verdict = await judge_case(judge, case, translation)
        rows.append(
            {
                "id": case.id,
                "source": case.source,
                "translation": translation,
                **verdict,
            }
        )
        status = "PASS" if verdict["pass"] else "FAIL"
        print(f"  {status} score={verdict['score']} translation={translation}")
        print(f"  reason={verdict['reason']}")

    passed = sum(1 for row in rows if row["pass"])
    pass_rate = passed / len(rows) if rows else 0
    avg_score = mean(row["score"] for row in rows) if rows else 0

    summary = {
        "total": len(rows),
        "passed": passed,
        "pass_rate": round(pass_rate, 3),
        "avg_score": round(avg_score, 2),
        "results": rows,
    }

    output_path = ROOT / "eval" / "llm_judge_results.json"
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Summary ===")
    print(f"pass_rate: {passed}/{len(rows)} = {pass_rate:.1%}")
    print(f"avg_score: {avg_score:.2f}/5")
    print(f"saved: {output_path}")

    return 0 if pass_rate >= 0.8 and avg_score >= 4.0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LLM-as-a-judge regression tests.")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N cases.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(run(limit=args.limit)))
