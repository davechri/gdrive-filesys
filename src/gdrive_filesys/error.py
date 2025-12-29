import errno
"""
This module provides a mapping from HTTP status codes to POSIX errno values.
Attributes:
    http_to_errno_map (dict): Maps HTTP status codes to corresponding errno values.
Functions:
    httpToErrno(status: int) -> int:
        Converts an HTTP status code to a POSIX errno value.
        If the status code is not explicitly mapped, returns errno.EAGAIN.
"""
http_to_errno_map = {
    # Client errors
    400: errno.EINVAL,  # Bad Request - Invalid argument
    401: errno.EACCES,  # Unauthorized - Permission denied
    403: errno.EPERM,   # Forbidden - Operation not permitted
    404: errno.ENOENT,  # Not Found - No such file or directory
    408: errno.ETIMEDOUT, # Request Timeout - Connection timed out

    # Server errors
    500: errno.EIO,     # Internal Server Error - Input/output error
    502: errno.EAGAIN,  # Bad Gateway - Try again (temporary error)
    503: errno.EAGAIN,  # Service Unavailable - Try again (temporary error)
    504: errno.ETIMEDOUT # Gateway Timeout - Connection timed out
}

def httpToErrno(status: int):
    if status in http_to_errno_map:
        return http_to_errno_map[status]
    else:
        return errno.EAGAIN