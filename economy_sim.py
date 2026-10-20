# -*- coding: utf-8 -*-
"""
economy_sim.py
================================================================================
《金铲铲之战》经济系统 · 局内金币结算规则复现
================================================================================
用途：把《系统拆解文档-金铲铲之战·经济系统》(V0.1, 画之灵切片) 中「金币来源 /
      去向 / 结算时序 / 边界行为」的规则，用纯标准库 Python 实现一遍，
      用于验证规则还原的准确性（策划作品集项目）。

作者：王雅轩（规则来源） / 本文件为规则复现实现
依赖：仅 Python 标准库（dataclasses / typing / unicodedata / sys），无第三方依赖
运行：python economy_sim.py
================================================================================

【规则章节索引】—— 代码中所有「对应拆解文档 §X.X」均指向本文件所依据的原文：
  §5.1   主流程 / 表 5.1.1 三类回合金币行为差异 / 表 5.1.2 单回合事项
  §6.1.1 金币基础来源（基础收入 / 胜利 / 连胜连败 / 利息）
  §6.2.1 金币一般去向（刷新商店 / 购买弈子 / 购买经验）
  §6.3   强化符文改变机制（利息上限改写 / 刷新改价…）
  §6.4   结算时序
  §6.5   边界与异常行为（金币不足 / 等级升满）
  §8.1   程序存储数据（12 个运行时字段）
  §8.2   配置表格结构（表A round_reward_config / 表B streak_reward_config）

【有意不实现的内容（及理由）】
  1. 弈子购买与三连合成 —— 涉及卡池概率与商店随机性（§6.2.1 购买弈子、§6.1.1
     二星/三星售卖表），其随机性属于「商店系统」而非「经济结算时序」，与本次
     要验证的金币结算规则无关，故不实现。本实现只把购买行为抽象为「一次扣费」。
     注：若后续需要验证卖出的盈亏表（§6.1.1 表 6.1.1/6.1.2），可另建单元测试，
     不并入本结算器。
  2. 战斗过程模拟 —— 胜负是「战斗结算系统」的输出（§3 数据流向：战斗 → 本系统），
     对经济系统而言是**输入参数**。本实现把 win/lose 作为 play_round() 的入参传入，
     不模拟战斗过程。
  3. 装备与弈子机制的金币收益 —— 法球（§6.1.2）、弈子机制（§6.1.3）、经济装备
     （§6.1.4）、克格莫投喂（§6.2.2）等属于「外挂产出/外挂消耗」，其触发条件与
     结算时序无固定关系，不参与 §6.1.1 + §6.4 定义的核心结算时序，故不实现。

【选秀回合的连胜/连败口径（已实机确认）】
  · 表 5.1.1 对选秀回合各列记「-」，按「经济完全不结算」理解：选秀回合既不增加
    也不清零连胜/连败计数，选秀前后的场次连续累计。实现见
    EconomySystem._apply_draft_round()。
  · 基础收入同样受 §6.1.1「1-3 回合开始发放」限定：1-2 回合为 0。
================================================================================
"""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# ==============================================================================
# 0. 常量定义
# ==============================================================================

# 回合类型（对应拆解文档 §5.1 / 表 5.1.1）
ROUND_TYPE_PVP = "PVP"      # 玩家对战：金币行为完整（基础/利息/胜利/连胜连败全结算）
ROUND_TYPE_PVE = "PVE"      # 野怪回合：有基础收入与利息，无胜利奖励、不结算连胜/连败
ROUND_TYPE_DRAFT = "DRAFT"  # 选秀回合：商店不可用，经济完全不结算

ROUND_TYPE_CN = {
    ROUND_TYPE_PVP: "PVP",
    ROUND_TYPE_PVE: "PVE",
    ROUND_TYPE_DRAFT: "选秀",
}

# 胜负结果
RESULT_WIN = "win"
RESULT_LOSE = "lose"
RESULT_NONE = "none"        # 野怪 / 选秀回合记 none（§8.1 last_result 字段说明）

# 金币去向单价（对应拆解文档 §6.2.1 一般去向）
REFRESH_COST = 2            # 刷新商店：每次 2 金
EXP_PURCHASE_COST = 4       # 购买经验：每次 4 金
EXP_PURCHASE_GAIN = 4       # 购买经验：一次获得 4 点经验值

# 利息参数（对应拆解文档 §6.1.1 利息）
INTEREST_STEP = 10          # 每满 10 金给 1 金利息
INTEREST_CAP_DEFAULT = 5    # 利息上限默认 5（可被强化符文改写，§6.3）

# 玩家等级上限（对应拆解文档 §6.5 等级升满：满级后按钮变「满级」并提示「已满」）
MAX_PLAYER_LEVEL = 10

# 各等级升级所需经验（「示意值」）
# 说明：拆解文档 §6.2.1 仅规定「4 金 = 4 经验」，**未给出**各等级升级所需经验曲线。
#       此处采用常见对局经验曲线的简化示意值，仅用于驱动 player_level 字段变化，
#       不影响任何金币结算结果，属实现层补全，不构成对文档规则的还原。
EXP_TO_NEXT_LEVEL: Dict[int, int] = {
    1: 2, 2: 2, 3: 6, 4: 10, 5: 20, 6: 36, 7: 56, 8: 80, 9: 100,
}


# ==============================================================================
# 1. 配置表 dataclass（对应拆解文档 §8.2 配置表格结构）
# ==============================================================================

@dataclass(frozen=True)
class RoundRewardConfig:
    """表 A：回合收益配置表 round_reward_config（对应拆解文档 §8.2 表 A）

    字段与文档一致：round_id / base_income / interest_step / interest_cap。
    """
    round_id: str            # 回合编号，如 "3-2"
    base_income: int         # 该回合基础收入
    interest_step: int = INTEREST_STEP      # 每多少金币给 1 金币利息
    interest_cap: int = INTEREST_CAP_DEFAULT  # 利息上限默认值


@dataclass(frozen=True)
class StreakRewardConfig:
    """表 B：连胜/连败档位表 streak_reward_config（对应拆解文档 §8.2 表 B）

    档位规则（§6.1.1）：2-4 场 +1 金、5 场 +2 金、6 场及以上 +3 金。
    用 [min_streak, max_streak] 闭区间表达，「及以上」档位的 max_streak 取一个大数。
    """
    min_streak: int          # 场次下限
    max_streak: int          # 场次上限（「及以上」档位填 STREAK_UNBOUNDED）
    gold: int                # 奖励金币

    def match(self, streak: int) -> bool:
        return self.min_streak <= streak <= self.max_streak


STREAK_UNBOUNDED = 10 ** 9   # 表示「6 场及以上」中的「及以上」

# 默认连胜/连败档位表（对应拆解文档 §6.1.1 连胜/连败）
DEFAULT_STREAK_CONFIGS: List[StreakRewardConfig] = [
    StreakRewardConfig(2, 4, 1),              # 2-4 场：+1 金
    StreakRewardConfig(5, 5, 2),              # 5 场：+2 金
    StreakRewardConfig(6, STREAK_UNBOUNDED, 3),  # 6 场及以上：+3 金
]


def _base_income_from_round_id(round_id: str) -> int:
    """由回合编号推导基础收入（对应拆解文档 §6.1.1 基础收入）。

    规则（§6.1.1 原文）：
        · 1-3 回合开始发放
        · 1-3 回合：2 金
        · 1-4 回合：3 金
        · 2-1 回合：4 金
        · 2-2 回合及以后：每回合 5 金

    注意：「1-3 回合开始发放」本身即限定条件——1-2 回合尚未进入发放区间，
    基础收入为 0，而不是把 1-2、1-3 并成同一档。
    """
    major, minor = (int(x) for x in round_id.split("-"))
    stage = major * 10 + minor          # 归一化为可直接比较的整数，如 1-3 -> 13
    if stage <= 12:                     # 1-2：尚未开始发放
        return 0
    if stage == 13:                     # 1-3
        return 2
    if stage == 14:                     # 1-4
        return 3
    if stage == 21:                     # 2-1
        return 4
    return 5                            # 2-2 及以后


def round_type_of(round_id: str) -> str:
    """按实机回合分布判断回合类型（§5.1 表 5.1.1）。

    · 1-2 / 1-3 / 1-4：野怪回合（PVE）——有基础收入与利息，无胜利奖励、不结算连胜/连败
    · 1 阶段之外的 X-4：选秀回合（DRAFT）——商店不可用，经济完全不结算
    · 其余：玩家对战（PVP）——全结算

    说明：1 阶段的第 4 回合是野怪而非选秀，选秀自 2-4 起出现。
    """
    major, minor = (int(x) for x in round_id.split("-"))
    if major == 1 and minor in (2, 3, 4):
        return ROUND_TYPE_PVE
    if minor == 4:
        return ROUND_TYPE_DRAFT
    return ROUND_TYPE_PVP


def build_default_round_configs(
    majors: Sequence[int] = (1, 2, 3, 4, 5),
    minors: Sequence[int] = (1, 2, 3, 4, 5, 6, 7),
) -> Dict[str, RoundRewardConfig]:
    """批量生成默认回合配置表（对应拆解文档 §8.2 表 A）。"""
    configs: Dict[str, RoundRewardConfig] = {}
    for m in majors:
        for n in minors:
            rid = f"{m}-{n}"
            configs[rid] = RoundRewardConfig(
                round_id=rid,
                base_income=_base_income_from_round_id(rid),
                interest_step=INTEREST_STEP,
                interest_cap=INTEREST_CAP_DEFAULT,
            )
    return configs


# ==============================================================================
# 2. 运行时状态（对应拆解文档 §8.1 程序存储数据 · 12 个字段）
# ==============================================================================

@dataclass
class RuntimeState:
    """对局运行时状态，字段与拆解文档 §8.1 一一对应（共 12 个字段）。

    文档 §8.1 说明：除 interest_gain 为「实时计算的派生值（不入库）」外，
    其余字段均存储于「对局内存」，其中 interest_cap 可被局内事件（强化符文）改写。
    """
    gold: int = 0                       # 1. gold 当前持有金币（初始 0）
    round_id: str = "1-2"               # 2. round_id 回合编号
    last_result: str = RESULT_NONE      # 3. last_result 上一回合胜负（win/lose/none）
    interest_gain: int = 0              # 4. interest_gain 本回合利息（派生值，不入库）
    interest_cap: int = INTEREST_CAP_DEFAULT   # 5. interest_cap 利息上限（可被强化符文改写）
    win_streak: int = 0                 # 6. win_streak 连胜场次
    lose_streak: int = 0                # 7. lose_streak 连败场次
    cumulative_session_rewards: int = 0  # 8. cumulative_session_rewards 连胜/连败累计奖励
    base_income: int = 2                # 9. base_income 本回合基础收入
    exp: int = 0                        # 10. exp 当前经验值
    player_level: int = 1               # 11. player_level 玩家等级
    refresh_count: int = 0              # 12. refresh_count 刷新次数（供「常客优惠」等符文使用）


# ==============================================================================
# 3. 强化符文改写器（对应拆解文档 §6.3 强化符文改变机制）
# ==============================================================================

class GameModifier:
    """强化符文改写器（对应拆解文档 §6.3 强化符文改变机制）。

    文档 §6.3 表 6.3.1 把符文对经济机制的改写分为「金币来源 / 金币去向 / 金币项参数」。
    本类实现其中**两类**「金币项参数 / 金币去向」的改写：

      1) 利息上限改写（金币项参数）—— 例：「利滚利加强版」把最大利息从 5 提升到 7。
         来源：§6.3 表 6.3.1 —— 官方描述「获得 18 金币，最大利息提升至 7 金币」。
      2) 刷新改价（金币去向）—— 例：「常客优惠」在刷新商店 8 次之后，每次仅花费 1 金币。
         来源：§6.3 表 6.3.1 —— 官方描述「在你刷新商店 8 次之后，每次刷新仅花费 1 金币」。

    设计上把「改写的效果」与「一次性即时收益」解耦：本类只改写**持续性规则参数**，
    「获得 18 金币」这类一次性发放属于即时收益，不属于结算规则改写，故不纳入本类
    （对应 §6.3 中「金币来源 · 无条件一次性发放」一栏，属于外挂产出）。
    """

    def __init__(
        self,
        name: str = "无",
        interest_cap: Optional[int] = None,
        refresh_price_rule: Optional[tuple] = None,
    ) -> None:
        # 符文名称（无符文时为「无」）
        self.name = name
        # 利息上限改写值；None 表示不改写（沿用配置表默认值 5）
        self.interest_cap = interest_cap
        # 刷新改价规则：(已刷新次数阈值, 之后每次花费)；None 表示不改价
        self.refresh_price_rule = refresh_price_rule

    # ---- 两种具体符文的构造器 ----

    @classmethod
    def li_gun_gun_plus(cls) -> "GameModifier":
        """「利滚利加强版」：利息上限 5 -> 7（§6.3 表 6.3.1 金币项参数）。"""
        return cls(name="利滚利加强版", interest_cap=7)

    @classmethod
    def frequent_guest(cls) -> "GameModifier":
        """「常客优惠」：刷新商店 8 次之后，每次刷新仅花费 1 金（§6.3 表 6.3.1 金币去向）。"""
        return cls(name="常客优惠", refresh_price_rule=(8, 1))

    # ---- 改写应用接口 ----

    def apply_interest_cap(self, base_cap: int) -> int:
        """把利息上限按符文改写后返回（§6.3 金币项参数）。"""
        return self.interest_cap if self.interest_cap is not None else base_cap

    def refresh_cost(self, refresh_count: int, base_cost: int = REFRESH_COST) -> int:
        """按已刷新次数返回本次刷新花费（§6.3 金币去向 · 刷新改价）。

        refresh_count 为「本次刷新之前已完成的刷新次数」。
        「8 次之后」=> 第 9 次起（refresh_count >= 8）每次仅 1 金。
        """
        if self.refresh_price_rule is not None:
            threshold, new_cost = self.refresh_price_rule
            if refresh_count >= threshold:
                return new_cost
        return base_cost

    def describe(self) -> str:
        parts = []
        if self.interest_cap is not None:
            parts.append(f"利息上限改写为 {self.interest_cap}")
        if self.refresh_price_rule is not None:
            th, c = self.refresh_price_rule
            parts.append(f"刷新满 {th} 次后每次 {c} 金")
        return f"{self.name}" + (f"（{'；'.join(parts)}）" if parts else "（无改写）")


# ==============================================================================
# 4. 控制台表格渲染工具（中文字符按 2 个显示宽度计算）
# ==============================================================================

def display_width(text: str) -> int:
    """计算字符串的终端显示宽度：东亚宽字符（W/F）按 2 计，其余按 1 计。

    这是保证含中文的表格列对齐的关键——len() 会把「回合」算成 2，实际占 4 格。
    """
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度把 text 填充到 width，支持 left/right/center 对齐。"""
    text = str(text)
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


# ==============================================================================
# 5. 经济系统主类
# ==============================================================================

class EconomySystem:
    """《金铲铲之战》经济系统结算器（对应拆解文档 §6 详细功能描述）。

    职责：按「五阶段」推进单个回合，并结算该回合金币（§5.1 表 5.1.2 / §6.4 结算时序）。
    """

    #: 结算日志表的列定义：(表头, 行数据 key, 对齐方式)
    COLUMNS = [
        ("回合", "round", "center"),
        ("类型", "rtype", "center"),
        ("胜负", "result", "center"),
        ("基础收入", "base", "right"),
        ("利息", "interest", "right"),
        ("胜利", "victory", "right"),
        ("连胜/连败奖励", "streak_reward", "right"),
        ("场次", "streak_label", "center"),
        ("收入合计", "income", "right"),
        ("支出", "spend", "right"),
        ("结余", "gold", "right"),
    ]

    def __init__(
        self,
        modifier: Optional[GameModifier] = None,
        round_configs: Optional[Dict[str, RoundRewardConfig]] = None,
        streak_configs: Optional[List[StreakRewardConfig]] = None,
    ) -> None:
        self.state = RuntimeState()
        self.modifier = modifier or GameModifier()
        self.round_configs = round_configs or build_default_round_configs()
        self.streak_configs = streak_configs or list(DEFAULT_STREAK_CONFIGS)
        self.log: List[dict] = []   # 每回合的结算记录，供 render_table() 渲染

    # --------------------------------------------------------------------------
    # 5.1 对外操作：金币去向（对应拆解文档 §6.2.1）
    # --------------------------------------------------------------------------

    def action_refresh(self) -> bool:
        """备战期操作：刷新商店，消耗金币（对应拆解文档 §6.2.1 商店刷新）。

        返回 True 表示刷新成功；False 表示被拦截。
        边界行为（§6.5 金币不足）：金币不足时**不扣费、不增加计数**。
        强化符文「常客优惠」会改写刷新单价（§6.3 金币去向 · 刷新改价）。
        """
        cost = self.modifier.refresh_cost(self.state.refresh_count, REFRESH_COST)
        if self.state.gold < cost:
            # §6.5 金币不足：拦截操作，不扣费、不增加 refresh_count
            return False
        self.state.gold -= cost
        self.state.refresh_count += 1     # 刷新次数仅在成功时累加（§8.1 refresh_count）
        return True

    def action_buy_exp(self) -> bool:
        """备战期操作：购买经验，4 金换 4 点经验（对应拆解文档 §6.2.1 购买经验/升级）。

        返回 True 表示购买成功；False 表示被拦截。
        边界行为：
          · §6.5 金币不足 —— 不扣费；
          · §6.5 等级升满 —— 满级后按钮变「满级/已满」，不可再购买。
        """
        if self.state.player_level >= MAX_PLAYER_LEVEL:
            return False                  # §6.5 等级升满：拦截
        if self.state.gold < EXP_PURCHASE_COST:
            return False                  # §6.5 金币不足：拦截，不扣费
        self.state.gold -= EXP_PURCHASE_COST
        self.state.exp += EXP_PURCHASE_GAIN
        self._try_level_up()
        return True

    def _try_level_up(self) -> None:
        """按经验阈值升级（经验曲线为示意值，见文件头 EXP_TO_NEXT_LEVEL 说明）。"""
        while self.state.player_level < MAX_PLAYER_LEVEL:
            need = EXP_TO_NEXT_LEVEL.get(self.state.player_level)
            if need is None or self.state.exp < need:
                break
            self.state.exp -= need
            self.state.player_level += 1

    # --------------------------------------------------------------------------
    # 5.2 核心方法：五阶段推进一个回合（对应拆解文档 §5.1 表 5.1.2 / §6.4）
    # --------------------------------------------------------------------------

    def play_round(
        self,
        round_id: str,
        round_type: str = ROUND_TYPE_PVP,
        result: Optional[str] = None,
        actions: Optional[Sequence[str]] = None,
    ) -> dict:
        """推进一个回合并结算金币，返回该回合的结算行（dict）。

        参数：
            round_id   : 回合编号，如 "3-2"（用于查配置表取基础收入）
            round_type : PVP / PVE / DRAFT（§5.1 表 5.1.1 三类回合）
            result     : 战斗结果 win/lose；PVE/选秀回合忽略（§3：战斗 -> 本系统）
            actions    : 备战期玩家操作序列，元素为 "refresh" / "buy_exp"

        五阶段（§5.1 表 5.1.2）：
            ① 回合开始 —— 发放基础收入
            ② 备战期   —— 玩家操作（刷新 / 购买经验，可继续消耗金币）
            ③ 对战     —— 胜负由外部（战斗系统）作为输入传入
            ④ 回合结算 —— 结算利息 / 胜利奖励 / 连胜连败奖励（§6.4 结算节点）
            ⑤ 回合结束 —— 更新 last_result，回合结束
        """
        self.state.round_id = round_id

        # ---- 选秀回合：商店不可用，经济完全不结算（§5.1 表 5.1.1，整行均为「-」）----
        if round_type == ROUND_TYPE_DRAFT:
            return self._apply_draft_round(round_id)

        # ============ ① 回合开始：发放基础收入（§6.1.1 基础收入）============
        cfg = self.round_configs.get(round_id)
        base_income = cfg.base_income if cfg else _base_income_from_round_id(round_id)
        self.state.base_income = base_income
        self.state.gold += base_income

        # ============ ② 备战期：玩家操作（§6.2.1 金币去向）============
        gold_before_actions = self.state.gold   # 用于统计本回合「支出」
        for act in (actions or []):
            if act == "refresh":
                self.action_refresh()
            elif act == "buy_exp":
                self.action_buy_exp()
            else:
                raise ValueError(f"未知的玩家操作: {act!r}")
        spend = gold_before_actions - self.state.gold   # 本回合实际支出（被拦截的操作不计入）

        # ============ ③ 对战：胜负由外部传入（§3 数据流向：战斗 -> 本系统）============
        # 本实现不模拟战斗过程；result 即为该阶段的结果输入。

        # ============ ④ 回合结算（§6.4 结算时序）============
        # 结算 1：胜利奖励（§6.1.1 胜利）——仅 PVP 回合
        victory = self._settle_victory(round_type, result)
        # 结算 2：连胜/连败（§6.1.1 连胜/连败）——仅 PVP 回合记计数并给奖励
        streak_reward, streak_label = self._settle_streak(round_type, result)
        # 结算 3：利息（§6.1.1 利息）——按【回合结算时】持有金币计算（玩家操作之后）
        interest = self._settle_interest(cfg)

        # 利息 + 胜利 + 连胜连败奖励一次性入账（§6.4：结算点 2）
        self.state.gold += interest + victory + streak_reward
        self.state.interest_gain = interest          # 派生值，仅供本回合展示（§8.1）
        self.state.cumulative_session_rewards += streak_reward

        # ============ ⑤ 回合结束 ============
        # last_result：野怪/选秀等回合记 none（§8.1 last_result 字段说明）
        self.state.last_result = result if round_type == ROUND_TYPE_PVP else RESULT_NONE

        row = self._make_row(
            round_id=round_id,
            round_type=round_type,
            result=result if round_type == ROUND_TYPE_PVP else RESULT_NONE,
            base=base_income,
            interest=interest,
            victory=victory,
            streak_reward=streak_reward,
            streak_label=streak_label,
            spend=spend,
        )
        self.log.append(row)
        return row

    # --------------------------------------------------------------------------
    # 5.3 各结算项的实现
    # --------------------------------------------------------------------------

    def _settle_interest(self, cfg: Optional[RoundRewardConfig]) -> int:
        """利息结算（对应拆解文档 §6.1.1 利息）。

        规则：每满 10 金获得 1 金利息，不足 10 的部分不计；上限默认 5 金，
              可被强化符文改写（§6.3）。
        口径：按【回合结算时】持有金币计算——即玩家备战期操作**之后**的余额，
              而非回合开始的余额。
        边界：金币为 0 或负数时利息为 0（§6.5 边界与异常行为）。
        """
        step = cfg.interest_step if cfg else INTEREST_STEP
        base_cap = cfg.interest_cap if cfg else INTEREST_CAP_DEFAULT
        cap = self.modifier.apply_interest_cap(base_cap)   # §6.3 利息上限改写
        self.state.interest_cap = cap                      # 上限改写后持久生效（§8.1 第 5 项）

        gold = self.state.gold
        if gold <= 0:
            return 0                                       # 金币为 0 或负数 -> 利息 0
        return min(gold // step, cap)

    def _settle_victory(self, round_type: str, result: Optional[str]) -> int:
        """胜利奖励结算（对应拆解文档 §6.1.1 胜利 / 表 5.1.1）。

        规则：该回合胜利 +1 金；PVE（野怪）与选秀回合不结算（表 5.1.1 中「胜利」列记 0）。
        """
        if round_type != ROUND_TYPE_PVP:
            return 0
        return 1 if result == RESULT_WIN else 0

    def _settle_streak(self, round_type: str, result: Optional[str]):
        """连胜/连败结算（对应拆解文档 §6.1.1 连胜/连败 / 表 5.1.1）。

        规则：
          · 场数达到 2-4 场 +1 金、达到 5 场 +2 金、达到 6 场及以上 +3 金；
          · 连胜与连败互斥：一方增加时另一方归零；
          · PVE 回合「不结算连胜/连败」（表 5.1.1 中「记计数」「奖励」两列均记 0）——
            即野怪回合既不记计数、也不给奖励，连胜/连败链原样保留。
        返回 (奖励金币, 场次标签)。
        """
        if round_type != ROUND_TYPE_PVP:
            # 野怪回合：不记计数、不给奖励（连胜/连败链不受影响）
            cur = self.state.win_streak or self.state.lose_streak
            return 0, self._streak_label(cur, self.state.win_streak > 0)

        if result == RESULT_WIN:
            self.state.win_streak += 1
            self.state.lose_streak = 0        # 互斥：连胜增加 -> 连败归零
        elif result == RESULT_LOSE:
            self.state.lose_streak += 1
            self.state.win_streak = 0         # 互斥：连败增加 -> 连胜归零
        else:
            return 0, "—"                     # 无有效结果（如 result=None）

        streak = self.state.win_streak or self.state.lose_streak
        reward = self._streak_reward(streak)
        label = self._streak_label(streak, self.state.win_streak > 0)
        return reward, label

    def _streak_reward(self, streak: int) -> int:
        """按档位表查询连胜/连败奖励（对应拆解文档 §6.1.1 / §8.2 表 B）。"""
        for cfg in self.streak_configs:
            if cfg.match(streak):
                return cfg.gold
        return 0

    @staticmethod
    def _streak_label(streak: int, is_win: bool) -> str:
        if streak <= 0:
            return "—"
        return f"{streak}连胜" if is_win else f"{streak}连败"

    def _apply_draft_round(self, round_id: str) -> dict:
        """选秀回合处理（对应拆解文档 §5.1 表 5.1.1 中「X-4（选秀）」一栏）。

        文档表 5.1.1 对选秀回合的「玩家可花费 / 基础收入 / 利息 / 胜利 / 记计数 /
        奖励」各列**全部记为「-」**，并在 §4 说明选秀回合属于「不可操作阶段」。
        因此本回合：不发放基础收入、不结算利息、不结算胜利、不结算连胜/连败，
        且商店不可用（不发放入任何玩家操作入口）。

        ---- 选秀对连胜/连败的影响（实机口径）----
        「经济完全不结算」包含连胜/连败：选秀回合既不增加也不清零计数，
        选秀前后的场次连续累计（例：选秀前 3 连败，选秀后下一场 PVP 输 → 4 连败）。
        """
        streak_before = self.state.win_streak or self.state.lose_streak
        self.state.interest_gain = 0
        self.state.last_result = RESULT_NONE       # §8.1：选秀回合记 none

        row = self._make_row(
            round_id=round_id,
            round_type=ROUND_TYPE_DRAFT,
            result=RESULT_NONE,
            base=0,
            interest=0,
            victory=0,
            streak_reward=0,
            streak_label="—",
            spend=0,
            note=f"经济不结算（连胜/连败计数保留在 {streak_before} 场）",
        )
        self.log.append(row)
        return row

    # --------------------------------------------------------------------------
    # 5.4 结算行与表格渲染
    # --------------------------------------------------------------------------

    def _make_row(self, **kw) -> dict:
        """构造一行结算记录（dict），供 render_table() 渲染。"""
        base = kw["base"]
        interest = kw["interest"]
        victory = kw["victory"]
        streak_reward = kw["streak_reward"]
        row = {
            "round": kw["round_id"],
            "rtype": ROUND_TYPE_CN.get(kw["round_type"], kw["round_type"]),
            "result": {RESULT_WIN: "胜", RESULT_LOSE: "负", RESULT_NONE: "—"}.get(
                kw["result"], "—"
            ),
            "base": base,
            "interest": interest,
            "victory": victory,
            "streak_reward": streak_reward,
            "streak_label": kw["streak_label"],
            "income": base + interest + victory + streak_reward,
            "spend": kw["spend"],
            "gold": self.state.gold,
            "note": kw.get("note", ""),
        }
        return row

    def render_table(self, rows: Optional[Sequence[dict]] = None, title: Optional[str] = None) -> str:
        """把结算日志渲染成对齐的控制台表格并打印，返回表格字符串。

        对齐要点：中文按 2 个显示宽度计算（display_width），保证列对齐。
        """
        rows = list(self.log if rows is None else rows)
        columns = self.COLUMNS

        # 计算每列的显示宽度 = max(表头宽度, 各单元格宽度)
        widths = []
        for header, key, _ in columns:
            w = display_width(header)
            for r in rows:
                w = max(w, display_width(str(r.get(key, ""))))
            widths.append(w)

        # 边框绘制字符
        top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
        mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
        bottom = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"

        def render_line(cells: Sequence[str], aligns: Sequence[str]) -> str:
            return "│" + "│".join(
                " " + _pad(c, w, a) + " " for c, w, a in zip(cells, widths, aligns)
            ) + "│"

        lines: List[str] = []
        if title:
            lines.append(title)
        header_aligns = ["center"] * len(columns)
        lines.append(top)
        lines.append(render_line([c[0] for c in columns], header_aligns))
        lines.append(mid)
        data_aligns = [c[2] for c in columns]
        for r in rows:
            lines.append(render_line([str(r.get(c[1], "")) for c in columns], data_aligns))
            lines.append(mid)
        lines[-1] = bottom          # 末行分隔线替换为底边框

        table = "\n".join(lines)
        print(table)
        return table

    def reset(self) -> None:
        """重置对局状态与日志（换一局重新模拟时使用）。"""
        self.state = RuntimeState()
        self.log = []


# ==============================================================================
# 6. 验证辅助
# ==============================================================================

def check(label: str, actual, expected) -> bool:
    """打印一条断言结果，返回是否通过。"""
    ok = actual == expected
    flag = "[OK]  " if ok else "[FAIL]"
    print(f"  {flag} {label}: 实际={actual!r} 期望={expected!r}")
    return ok


def banner(text: str) -> None:
    line = "=" * 78
    print("\n" + line)
    print(text)
    print(line)


# ==============================================================================
# 7. 内置验证场景
# ==============================================================================

def scenario_1_saving() -> bool:
    """场景一「存钱吃利息」（对应拆解文档 §6.1.1 利息 / §6.4 结算时序）。

    策略：13 个回合全程不刷新、不购买经验（actions 为空），把金币全部留存吃利息。
    回合类型按实机分布：1-2 / 1-3 / 1-4 为野怪回合，2-4 为选秀回合，其余为 PVP；
    PVP 回合预设全胜，使利息阶梯按 1 -> 2 -> 3 -> 4 -> 5 递增，
    并在 3-1 回合（计息时金币 68，原始利息 6）首次被上限 5 截断。
    """
    banner("场景一：存钱吃利息（13 回合不刷新 / 不买经验）")

    sysm = EconomySystem()
    rounds = ["1-2", "1-3", "1-4", "2-1", "2-2", "2-3", "2-4",
              "2-5", "2-6", "2-7", "3-1", "3-2", "3-3"]

    print("  回合类型：" + " ".join(
        f"{rid}({ROUND_TYPE_CN[round_type_of(rid)]})" for rid in rounds))
    print()

    # 逐回合推进；每次 play_round 前记录「计息时金币」——基础收入到账后、利息结算前的余额
    pre_settle_gold: List[int] = []
    for rid in rounds:
        snapshot = sysm.state.gold + sysm.round_configs[rid].base_income
        sysm.play_round(rid, round_type_of(rid), result="win", actions=[])
        pre_settle_gold.append(snapshot)

    print()
    sysm.render_table(title="  表 1：存钱策略逐回合金币结算日志")

    ok = True
    print()

    # ---- 验证：基础收入自 1-3 回合开始发放（§6.1.1）----
    ok &= check("1-2 回合基础收入为 0（「1-3 回合开始发放」）", sysm.log[0]["base"], 0)
    ok &= check("1-3 / 1-4 / 2-1 基础收入为 2 / 3 / 4",
                [sysm.log[i]["base"] for i in (1, 2, 3)], [2, 3, 4])

    # ---- 验证：野怪回合不给胜利奖励、不计场次（§5.1 表 5.1.1）----
    ok &= check("野怪回合无胜利奖励",
                [r["victory"] for r in sysm.log if r["rtype"] == ROUND_TYPE_CN[ROUND_TYPE_PVE]],
                [0, 0, 0])
    ok &= check("野怪回合不计场次",
                [r["streak_label"] for r in sysm.log if r["rtype"] == ROUND_TYPE_CN[ROUND_TYPE_PVE]],
                ["—", "—", "—"])

    # ---- 验证：利息阶梯 1 -> 2 -> 3 -> 4 -> 5 ----
    interests = [r["interest"] for r in sysm.log]
    ladder: List[int] = []
    for v in interests:
        if v not in ladder:
            ladder.append(v)
    ladder = [v for v in ladder if v > 0]
    print(f"  利息序列：{interests}")
    print(f"  利息阶梯（去重升序）：{ladder}")
    ok &= check("利息阶梯为 1->2->3->4->5", ladder, [1, 2, 3, 4, 5])
    ok &= check("上限值为 5（未改写）", max(interests), 5)

    # ---- 验证：原始利息超过 5 的部分被上限截断 ----
    cap_hits = [(g, r["interest"]) for g, r in zip(pre_settle_gold, sysm.log)
                if r["rtype"] != ROUND_TYPE_CN[ROUND_TYPE_DRAFT]
                and g // INTEREST_STEP > INTEREST_CAP_DEFAULT]
    print(f"  计息时金币对应的原始利息 > 5 的回合：{cap_hits}")
    ok &= check("原始利息超 5 的回合全部被截为 5", [i for _, i in cap_hits], [5, 5, 5])

    # ---- 验证：选秀回合不结算，且连胜/连败计数保留 ----
    draft_i = rounds.index("2-4")
    ok &= check("2-4 选秀回合收入合计为 0", sysm.log[draft_i]["income"], 0)
    ok &= check("选秀后场次由 3 连胜续为 4 连胜（计数保留）",
                sysm.log[draft_i + 1]["streak_label"], "4连胜")

    # 边界：金币为 0 / 负数时利息为 0（§6.5）
    probe = EconomySystem()
    probe.state.gold = 0
    ok &= check("金币 0 时利息为 0", probe._settle_interest(None), 0)
    probe.state.gold = -5
    ok &= check("金币负数时利息为 0", probe._settle_interest(None), 0)

    print(f"\n  场景一结论：{'全部通过' if ok else '存在失败项'}")
    return ok


def scenario_2_losing() -> bool:
    """场景二「计划连败局」（对应拆解文档 §6.1.1 连胜/连败 / §6.5 边界）。

    策略：1-2 / 1-3 / 1-4 野怪回合之后，连败 7 场 PVP——中间插入 2-4 选秀回合，
    随后转胜 2 场，末尾再走一次 3-4 选秀。
    验证：
      · 档位在 2/3/4 场 +1、5 场 +2、6 场及以上 +3 正确切换；
      · 连胜与连败互斥（转胜后连败归零，连胜从 1 重新计）；
      · 选秀回合经济不结算，但连胜/连败计数保留（连败由 3 场续到 4 场）。
    """
    banner("场景二：计划连败局（连败档位切换 + 胜负互斥 + 选秀回合）")

    sysm = EconomySystem()
    rounds = ["1-2", "1-3", "1-4",
              "2-1", "2-2", "2-3", "2-4", "2-5", "2-6", "2-7", "3-1",
              "3-2", "3-3", "3-4"]
    lose_set = {"2-1", "2-2", "2-3", "2-5", "2-6", "2-7", "3-1"}
    win_set = {"3-2", "3-3"}

    for rid in rounds:
        res = "lose" if rid in lose_set else ("win" if rid in win_set else None)
        # 野怪 / 选秀回合的胜负参数不起作用，由 round_type_of() 判定
        sysm.play_round(rid, round_type_of(rid), result=res, actions=[])

    print()
    sysm.render_table(title="  表 2：连败策略逐回合金币结算日志")

    ok = True
    print()
    # 连败 7 回合的档位奖励序列：0,1,1,1,2,3,3
    lose_rewards = [r["streak_reward"] for r in sysm.log
                    if r["rtype"] == ROUND_TYPE_CN[ROUND_TYPE_PVP] and r["result"] == "负"][:7]
    ok &= check("连败 1->7 场奖励序列", lose_rewards, [0, 1, 1, 1, 2, 3, 3])

    # 场次标签
    print(f"  场次标签：{[r['streak_label'] for r in sysm.log]}")
    ok &= check("野怪回合不计场次（前 3 回合为 —）",
                [r["streak_label"] for r in sysm.log[:3]], ["—", "—", "—"])

    # 选秀回合（连败链中间）：经济不结算、计数保留
    draft_i = rounds.index("2-4")
    ok &= check("2-4 选秀回合收入合计为 0", sysm.log[draft_i]["income"], 0)
    ok &= check("选秀后连败由 3 场续为 4 场（计数保留）",
                sysm.log[draft_i + 1]["streak_label"], "4连败")

    # 胜负互斥：转胜回合后 win_streak=1、lose_streak=0
    win_i = rounds.index("3-2")
    ok &= check("转胜后连败归零", sysm.log[win_i]["streak_label"], "1连胜")
    ok &= check("连胜 2 场奖励 +1", sysm.log[win_i + 1]["streak_reward"], 1)

    # 末尾选秀：仍不结算，连胜计数保留
    ok &= check("3-4 选秀回合收入合计为 0", sysm.log[-1]["income"], 0)
    ok &= check("选秀后连胜计数仍为 2", sysm.state.win_streak, 2)

    # ---- 边界：金币不足时操作被拦截，不扣费、不增加计数（§6.5）----
    print()
    guard = EconomySystem()
    guard.state.gold = 1                     # 不足 2 金
    before = (guard.state.gold, guard.state.refresh_count)
    ret = guard.action_refresh()
    after = (guard.state.gold, guard.state.refresh_count)
    ok &= check("金币不足刷新被拦截（返回 False）", ret, False)
    ok &= check("金币不足刷新：不扣费、不加计数", after, before)

    print(f"\n  场景二结论：{'全部通过' if ok else '存在失败项'}")
    return ok


def scenario_3_modifier() -> bool:
    """场景三「强化符文改写」（对应拆解文档 §6.3 强化符文改变机制）。

    主验证：从 40 金起、装配「利滚利加强版」（利息上限 5 -> 7），连跑 8 个 PVP 回合，
            观察利息由 4 一路爬到 7，其中原始利息为 6 / 7 / 8 的回合都按改写后的上限结算；
            并以同起点、同胜负、不装符文的对照组，比对 8 回合后的金币差额。
    附加验证：「常客优惠」刷新改价（满 8 次后每次 1 金）。
    """
    banner("场景三：强化符文改写（利息上限 5 -> 7，连跑 8 回合）")

    rounds = ["4-1", "4-2", "4-3", "5-1", "5-2", "5-3", "5-4", "5-5"]

    # ---------- 主组：装配「利滚利加强版」 ----------
    mod = GameModifier.li_gun_gun_plus()
    print(f"  主组装配符文：{mod.describe()}")
    print(f"  起点金币 = 40，连跑 {len(rounds)} 个 PVP 回合（全胜）")

    sysm = EconomySystem(modifier=mod)
    sysm.state.gold = 40
    for rid in rounds:
        sysm.play_round(rid, ROUND_TYPE_PVP, result="win", actions=[])

    print()
    sysm.render_table(title="  表 3：装配「利滚利加强版」后的结算日志")

    # ---------- 对照组：同样起点、同样胜负，不装符文（默认上限 5）----------
    ctrl = EconomySystem()
    ctrl.state.gold = 40
    for rid in rounds:
        ctrl.play_round(rid, ROUND_TYPE_PVP, result="win", actions=[])

    ok = True
    print()
    interests = [r["interest"] for r in sysm.log]
    ctrl_interests = [r["interest"] for r in ctrl.log]
    print(f"  符文组利息序列：{interests}")
    print(f"  对照组利息序列：{ctrl_interests}")

    ok &= check("利息上限被改写为 7", sysm.state.interest_cap, 7)
    ok &= check("利息由 4 爬到 7（越过默认上限 5）",
                interests, [4, 5, 6, 7, 7, 7, 7, 7])
    ok &= check("原始利息为 6 的回合按 6 结算（默认上限只会给 5）",
                sysm.log[2]["interest"], 6)
    ok &= check("原始利息 ≥7 的回合全部按改写上限 7 结算",
                [sysm.log[i]["interest"] for i in range(3, 8)], [7] * 5)
    ok &= check("对照：默认上限下利息封顶在 5",
                ctrl_interests, [4, 5, 5, 5, 5, 5, 5, 5])
    ok &= check("8 回合后符文组多出 11 金",
                sysm.state.gold - ctrl.state.gold, 11)

    # ---------- 附加：常客优惠（刷新改价）----------
    print()
    print("  附加验证：装配「常客优惠」（刷新满 8 次后每次 1 金）")
    mod2 = GameModifier.frequent_guest()
    gsys = EconomySystem(modifier=mod2)
    gsys.state.gold = 40
    print(f"  符文：{mod2.describe()}；设定金币 = 40，连续刷新 10 次：")
    costs = []
    for i in range(1, 11):
        ok_refresh = gsys.action_refresh()
        cost = mod2.refresh_cost(gsys.state.refresh_count - 1, REFRESH_COST) if ok_refresh else 0
        costs.append(cost)
        print(f"    第 {i:>2} 次刷新 -> 花费 {cost} 金，剩余 {gsys.state.gold} 金，"
              f"累计刷新 {gsys.state.refresh_count} 次")
    ok &= check("前 8 次花费均为 2 金", costs[:8], [2] * 8)
    ok &= check("第 9、10 次花费均为 1 金", costs[8:], [1, 1])

    print(f"\n  场景三结论：{'全部通过' if ok else '存在失败项'}")
    return ok


# ==============================================================================
# 8. 入口
# ==============================================================================

def main() -> int:
    # Windows 控制台默认编码可能不是 UTF-8，这里做一次安全的重配置，
    # 保证中文与制表符（┌─┬┐）能正确输出。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("《金铲铲之战》经济系统 · 金币结算规则复现（economy_sim.py）")
    print("规则来源：《系统拆解文档-金铲铲之战·经济系统》V0.1（画之灵切片）")
    print("仅使用 Python 标准库，无第三方依赖。")

    results = [
        scenario_1_saving(),
        scenario_2_losing(),
        scenario_3_modifier(),
    ]

    banner("总览")
    names = ["场景一 存钱吃利息", "场景二 计划连败局", "场景三 强化符文改写"]
    for name, ok in zip(names, results):
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}")
    print(f"\n  合计：{sum(results)}/{len(results)} 个场景全部断言通过。")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
