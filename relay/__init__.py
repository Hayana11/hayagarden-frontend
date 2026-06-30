"""Relay 模块：能力声明 + 自动降级 + 统一请求层"""
from relay.capabilities import get_caps, detect_relay, CAPABILITIES
from relay.manager import relay

__all__ = ["relay", "get_caps", "detect_relay", "CAPABILITIES"]
