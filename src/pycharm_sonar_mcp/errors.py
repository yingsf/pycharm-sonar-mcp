"""稳定的、机器可读的错误码与统一异常层级

错误码是返回给 MCP 客户端的公开契约的一部分，必须保持稳定。
回溯信息绝不回传给模型，仅写入 stderr。
"""

from __future__ import annotations

from typing import Final

# --- 错误码（公开契约，保持稳定）---

IDE_NOT_FOUND: Final[str] = "IDE_NOT_FOUND"
IDE_PORT_INVALID: Final[str] = "IDE_PORT_INVALID"
IDE_PORT_OUT_OF_RANGE: Final[str] = "IDE_PORT_OUT_OF_RANGE"
IDE_MULTIPLE_MATCHES: Final[str] = "IDE_MULTIPLE_MATCHES"
IDE_NO_INSTANCE_INDEXES_FILE: Final[str] = "IDE_NO_INSTANCE_INDEXES_FILE"
IDE_AUTHORITY_REJECTED: Final[str] = "IDE_AUTHORITY_REJECTED"
IDE_IPV4_CONNECTION_FAILED: Final[str] = "IDE_IPV4_CONNECTION_FAILED"
IDE_INDEXING: Final[str] = "IDE_INDEXING"
IDE_RESTARTED: Final[str] = "IDE_RESTARTED"

FILE_NOT_FOUND: Final[str] = "FILE_NOT_FOUND"
FILE_NOT_REGULAR: Final[str] = "FILE_NOT_REGULAR"
FILE_NOT_INDEXED: Final[str] = "FILE_NOT_INDEXED"
FILE_TYPE_UNSUPPORTED: Final[str] = "FILE_TYPE_UNSUPPORTED"

WORKSPACE_NOT_CONFIGURED: Final[str] = "WORKSPACE_NOT_CONFIGURED"
WORKSPACE_VIOLATION: Final[str] = "WORKSPACE_VIOLATION"
SYMLINK_ESCAPE: Final[str] = "SYMLINK_ESCAPE"
MULTIPLE_PROJECT_ROOTS: Final[str] = "MULTIPLE_PROJECT_ROOTS"
TOO_MANY_FILES: Final[str] = "TOO_MANY_FILES"

GIT_INVALID_REPOSITORY: Final[str] = "GIT_INVALID_REPOSITORY"
GIT_INVALID_BASE_REF: Final[str] = "GIT_INVALID_BASE_REF"
GIT_COMMAND_FAILED: Final[str] = "GIT_COMMAND_FAILED"

SONAR_RATE_LIMITED: Final[str] = "SONAR_RATE_LIMITED"
SONAR_BAD_RESPONSE: Final[str] = "SONAR_BAD_RESPONSE"
SONAR_UNAVAILABLE: Final[str] = "SONAR_UNAVAILABLE"
BATCH_TIMEOUT: Final[str] = "BATCH_TIMEOUT"
BATCH_ERROR: Final[str] = "BATCH_ERROR"
ANALYSIS_PARTIAL_FAILURE: Final[str] = "ANALYSIS_PARTIAL_FAILURE"

PROXY_INTERFERENCE: Final[str] = "PROXY_INTERFERENCE"
BAD_REQUEST: Final[str] = "BAD_REQUEST"
INTERNAL_ERROR: Final[str] = "INTERNAL_ERROR"


class SonarMcpError(Exception):
    """所有暴露给 MCP 客户端的错误的基类

    Attributes:
        code: 稳定的错误码（见上方常量）。
        user_message: 面向模型/用户的可读、可操作消息。
    """

    code: str = INTERNAL_ERROR

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.user_message}"


# --- 具体错误工厂函数 ---


def ide_not_found(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_NOT_FOUND, message)


def ide_port_invalid(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_PORT_INVALID, message)


def ide_port_out_of_range(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_PORT_OUT_OF_RANGE, message)


def ide_multiple_matches(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_MULTIPLE_MATCHES, message)


def ide_no_instance_indexes_file(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_NO_INSTANCE_INDEXES_FILE, message)


def ide_authority_rejected(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_AUTHORITY_REJECTED, message)


def ide_ipv4_connection_failed(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_IPV4_CONNECTION_FAILED, message)


def ide_indexing(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_INDEXING, message)


def ide_restarted(message: str) -> SonarMcpError:
    return SonarMcpError(IDE_RESTARTED, message)


def file_not_found(message: str) -> SonarMcpError:
    return SonarMcpError(FILE_NOT_FOUND, message)


def file_not_regular(message: str) -> SonarMcpError:
    return SonarMcpError(FILE_NOT_REGULAR, message)


def file_not_indexed(message: str) -> SonarMcpError:
    return SonarMcpError(FILE_NOT_INDEXED, message)


def file_type_unsupported(message: str) -> SonarMcpError:
    return SonarMcpError(FILE_TYPE_UNSUPPORTED, message)


def workspace_not_configured(message: str) -> SonarMcpError:
    return SonarMcpError(WORKSPACE_NOT_CONFIGURED, message)


def workspace_violation(message: str) -> SonarMcpError:
    return SonarMcpError(WORKSPACE_VIOLATION, message)


def symlink_escape(message: str) -> SonarMcpError:
    return SonarMcpError(SYMLINK_ESCAPE, message)


def multiple_project_roots(message: str) -> SonarMcpError:
    return SonarMcpError(MULTIPLE_PROJECT_ROOTS, message)


def too_many_files(message: str) -> SonarMcpError:
    return SonarMcpError(TOO_MANY_FILES, message)


def git_invalid_repository(message: str) -> SonarMcpError:
    return SonarMcpError(GIT_INVALID_REPOSITORY, message)


def git_invalid_base_ref(message: str) -> SonarMcpError:
    return SonarMcpError(GIT_INVALID_BASE_REF, message)


def git_command_failed(message: str) -> SonarMcpError:
    return SonarMcpError(GIT_COMMAND_FAILED, message)


def sonar_rate_limited(message: str) -> SonarMcpError:
    return SonarMcpError(SONAR_RATE_LIMITED, message)


def sonar_bad_response(message: str) -> SonarMcpError:
    return SonarMcpError(SONAR_BAD_RESPONSE, message)


def sonar_unavailable(message: str) -> SonarMcpError:
    return SonarMcpError(SONAR_UNAVAILABLE, message)


def batch_timeout(message: str) -> SonarMcpError:
    return SonarMcpError(BATCH_TIMEOUT, message)


def batch_error(message: str) -> SonarMcpError:
    return SonarMcpError(BATCH_ERROR, message)


def analysis_partial_failure(message: str) -> SonarMcpError:
    return SonarMcpError(ANALYSIS_PARTIAL_FAILURE, message)


def proxy_interference(message: str) -> SonarMcpError:
    return SonarMcpError(PROXY_INTERFERENCE, message)


def bad_request(message: str) -> SonarMcpError:
    return SonarMcpError(BAD_REQUEST, message)


def internal_error(message: str) -> SonarMcpError:
    return SonarMcpError(INTERNAL_ERROR, message)
