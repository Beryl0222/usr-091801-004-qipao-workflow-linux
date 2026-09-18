"""领域错误：HTTP 边界统一据此映射状态码与错误体。"""


class DomainError(Exception):
    """所有可预期业务违规的基类。"""

    status = 400
    code = "domain_error"

    def to_body(self):
        return {"error": self.code, "message": str(self)}


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Conflict(DomainError):
    """状态机不允许当前操作。"""

    status = 409
    code = "conflict"


class VersionConflict(Conflict):
    """乐观并发版本冲突：两家门店同时修改同一订单时，后提交者得到此错误。"""

    code = "version_conflict"

    def __init__(self, stream, expected, actual):
        super().__init__(
            f"订单 {stream} 已被其他门店修改（期望版本 {expected}，当前版本 {actual}），请刷新后重试"
        )
        self.stream = stream
        self.expected = expected
        self.actual = actual

    def to_body(self):
        body = super().to_body()
        body["expected_version"] = self.expected
        body["actual_version"] = self.actual
        return body


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"


class ValidationFailed(DomainError):
    status = 422
    code = "validation_failed"
