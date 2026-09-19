"""领域错误类型。HTTP 层据此映射状态码。"""


class DomainError(Exception):
    """所有领域规则违反的基类。"""

    http_status = 422
    code = "domain_error"

    def __init__(self, message, *, code=None, details=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class ValidationError(DomainError):
    """请求数据不完整或不合规。"""

    http_status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    """引用的聚合或实体不存在。"""

    http_status = 404
    code = "not_found"


class ConflictError(DomainError):
    """乐观锁版本冲突或非法状态迁移。"""

    http_status = 409
    code = "conflict"


class AuthorizationError(DomainError):
    """角色无权读取或操作该资源。"""

    http_status = 403
    code = "forbidden"
