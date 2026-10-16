"""
风控限速器 (Rate Limiter)

实现抖音下载的风控保护策略：
- 串行下载（默认1并发）
- 智能间隔（基础3秒 + 随机抖动）
- 自适应降级（检测到异常时自动增加间隔）
- 账号保护（失败重试、冷却机制）
"""

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .logger import get_logger

logger = get_logger()


class RiskLevel(Enum):
    """风控等级"""

    NORMAL = 0  # 正常模式
    WARNING = 1  # 警告模式（检测到轻微异常）
    RESTRICTED = 2  # 限制模式（触发429/403）
    COOLDOWN = 3  # 冷却模式（连续风控）


@dataclass
class RateLimitConfig:
    """限速配置"""

    # 基础间隔（秒）
    base_interval: float = 3.0
    # 随机抖动比例（±30%）
    jitter_ratio: float = 0.3
    # 最大间隔（秒）
    max_interval: float = 60.0
    # 失败重试次数
    max_retries: int = 3
    # 失败重试基础间隔（秒）
    retry_base_interval: float = 10.0
    # 风控触发后冷却时间（秒）
    cooldown_duration: float = 300.0
    # 连续成功多少次后尝试降级
    success_threshold: int = 5
    # 连续失败多少次后升级风控
    failure_threshold: int = 3


class RateLimiter:
    """
    风控感知的限速器

    特性：
    - 根据风控等级动态调整下载间隔
    - 随机抖动避免请求模式化
    - 自动降级/升级风控等级
    - 统计下载成功率和风控触发次数
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()
        self.level = RiskLevel.NORMAL
        self.last_request_time = 0.0
        self.success_count = 0
        self.failure_count = 0
        self.cooldown_until = 0.0
        self._lock = asyncio.Lock()

        # 统计信息
        self.stats = {
            "total_requests": 0,
            "success_requests": 0,
            "failed_requests": 0,
            "rate_limited_count": 0,  # 触发429/403次数
            "current_level": self.level.name,
        }

    async def acquire(self, context: Optional[str] = None) -> bool:
        """
        获取下载许可

        Args:
            context: 上下文信息（如视频ID），用于日志

        Returns:
            True: 可以继续下载
            False: 处于冷却期，需要等待
        """
        async with self._lock:
            # 检查是否在冷却期
            current_time = time.time()
            if current_time < self.cooldown_until:
                wait_time = self.cooldown_until - current_time
                logger.warning(
                    f"处于风控冷却期，还需等待 {wait_time:.1f} 秒",
                    extra={
                        "component": "rate_limiter",
                        "wait_time": wait_time,
                        "level": self.level.name,
                    },
                )
                return False

            # 计算需要等待的时间
            interval = self._calculate_interval()
            elapsed = current_time - self.last_request_time

            if elapsed < interval:
                wait = interval - elapsed
                logger.debug(
                    f"限速等待: {wait:.1f} 秒",
                    extra={
                        "component": "rate_limiter",
                        "wait_time": wait,
                        "interval": interval,
                        "level": self.level.name,
                    },
                )
                await asyncio.sleep(wait)

            self.last_request_time = time.time()
            self.stats["total_requests"] += 1

            return True

    def _calculate_interval(self) -> float:
        """计算当前间隔时间"""
        # 基础间隔
        base_intervals = {
            RiskLevel.NORMAL: self.config.base_interval,
            RiskLevel.WARNING: self.config.base_interval * 1.5,
            RiskLevel.RESTRICTED: self.config.base_interval * 3,
            RiskLevel.COOLDOWN: self.config.max_interval,
        }

        base = base_intervals.get(self.level, self.config.base_interval)

        # 添加随机抖动
        jitter = base * self.config.jitter_ratio * (random.random() - 0.5)
        interval = base + jitter

        return min(interval, self.config.max_interval)

    def on_success(self, context: Optional[str] = None):
        """
        请求成功回调

        连续成功一定次数后尝试降级风控等级
        """
        self.success_count += 1
        self.failure_count = 0
        self.stats["success_requests"] += 1

        # 逐级降级
        if self.success_count >= self.config.success_threshold:
            if self.level.value > RiskLevel.NORMAL.value:
                old_level = self.level
                self.level = RiskLevel(self.level.value - 1)
                self.success_count = 0

                logger.info(
                    f"风控等级降级: {old_level.name} -> {self.level.name}",
                    extra={
                        "component": "rate_limiter",
                        "old_level": old_level.name,
                        "new_level": self.level.name,
                    },
                )

    def on_failure(
        self, status_code: Optional[int] = None, error: Optional[str] = None
    ):
        """
        请求失败回调

        根据错误类型升级风控等级
        """
        self.failure_count += 1
        self.success_count = 0
        self.stats["failed_requests"] += 1

        # 判断是否触发风控
        is_rate_limited = status_code in [429, 403]

        if is_rate_limited:
            self.stats["rate_limited_count"] += 1

            # 风控升级
            if self.level.value < RiskLevel.COOLDOWN.value:
                old_level = self.level
                self.level = RiskLevel(self.level.value + 1)

                logger.warning(
                    f"触发风控，等级升级: {old_level.name} -> {self.level.name}",
                    extra={
                        "component": "rate_limiter",
                        "status_code": status_code,
                        "old_level": old_level.name,
                        "new_level": self.level.name,
                        "error": error,
                    },
                )

                # 如果升级到冷却模式，设置冷却时间
                if self.level == RiskLevel.COOLDOWN:
                    self.cooldown_until = time.time() + self.config.cooldown_duration
                    logger.error(
                        f"进入风控冷却期，暂停 {self.config.cooldown_duration} 秒",
                        extra={
                            "component": "rate_limiter",
                            "cooldown_duration": self.config.cooldown_duration,
                        },
                    )

        elif self.failure_count >= self.config.failure_threshold:
            # 连续失败但未触发429/403，也升级风控
            if self.level.value < RiskLevel.RESTRICTED.value:
                old_level = self.level
                self.level = RiskLevel(self.level.value + 1)

                logger.warning(
                    f"连续失败，风控等级升级: {old_level.name} -> {self.level.name}",
                    extra={
                        "component": "rate_limiter",
                        "failure_count": self.failure_count,
                        "old_level": old_level.name,
                        "new_level": self.level.name,
                    },
                )

    def get_retry_delay(self, attempt: int) -> float:
        """
        获取重试延迟时间

        使用指数退避策略
        """
        base = self.config.retry_base_interval * (2**attempt)
        jitter = base * 0.2 * random.random()
        return base + jitter

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self.stats,
            "current_level": self.level.name,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": (
                self.stats["success_requests"] / max(self.stats["total_requests"], 1)
            ),
        }

    def reset(self):
        """重置限速器状态"""
        self.level = RiskLevel.NORMAL
        self.last_request_time = 0
        self.success_count = 0
        self.failure_count = 0
        self.cooldown_until = 0
        self.stats = {
            "total_requests": 0,
            "success_requests": 0,
            "failed_requests": 0,
            "rate_limited_count": 0,
            "current_level": self.level.name,
        }

        logger.info("限速器状态已重置", extra={"component": "rate_limiter"})


class AdaptiveRateLimiter:
    """
    自适应限速器（高级版本）

    根据历史下载数据自动调整限速参数
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()
        self.limiters: dict[str, RateLimiter] = {}

    def get_limiter(self, account_id: str = "default") -> RateLimiter:
        """
        获取指定账号的限速器

        支持多账号隔离，避免一个账号风控影响其他账号
        """
        if account_id not in self.limiters:
            self.limiters[account_id] = RateLimiter(self.config)
        return self.limiters[account_id]

    def get_global_stats(self) -> dict:
        """获取所有账号的统计信息"""
        return {
            account: limiter.get_stats() for account, limiter in self.limiters.items()
        }
