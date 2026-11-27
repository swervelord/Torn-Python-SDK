"""Generated from spec/shared/error-codes.json. Do not edit."""

from ._error_code_behavior import _ErrorCodeBehavior


class TornErrorCode(_ErrorCodeBehavior):
    UNKNOWN = 0
    KEY_EMPTY = 1
    INCORRECT_KEY = 2
    WRONG_TYPE = 3
    WRONG_FIELDS = 4
    TOO_MANY_REQUESTS = 5
    INCORRECT_ID = 6
    INCORRECT_ID_ENTITY_RELATION = 7
    IP_BLOCKED = 8
    API_DISABLED = 9
    KEY_OWNER_IN_FEDERAL_JAIL = 10
    KEY_CHANGE_COOLDOWN = 11
    KEY_READ_ERROR = 12
    KEY_TEMPORARY_DISABLED = 13
    DAILY_READ_LIMIT_REACHED = 14
    LOG_UNAVAILABLE = 15
    ACCESS_LEVEL_TOO_LOW = 16
    BACKEND_ERROR = 17
    API_KEY_PAUSED = 18
    MUST_MIGRATE_TO_CRIMES_V2 = 19
    RACE_NOT_FINISHED = 20
    INCORRECT_CATEGORY = 21
    ONLY_AVAILABLE_IN_API_V1 = 22
    ONLY_AVAILABLE_IN_API_V2 = 23
    CLOSED_TEMPORARILY = 24
    INVALID_STAT_REQUESTED = 25
    ONLY_CATEGORY_OR_STATS_ALLOWED = 26
    MUST_MIGRATE_TO_ORGANIZED_CRIMES_V2 = 27
    INCORRECT_LOG_ID = 28
    CATEGORY_SELECTION_UNAVAILABLE_FOR_INTERACTION_LOGS = 29
    FILE_DOES_NOT_EXIST = 30
    CITY_STATS_CRON_FAILED = 31
    ENDPOINT_CLOSED_UNTIL_ATTACKING_PERIOD = 32
    NO_COMPETITION_AVAILABLE = 33


ERROR_POLICY = {
    0: ('Unknown', 'unknown', 'Unknown error: Unhandled error, should not occur.', 'request', 'fail', 'none', False),
    1: ('KeyEmpty', 'key_empty', 'Key is empty: Private key is empty in current request.', 'key', 'fail', 'none', False),
    2: ('IncorrectKey', 'incorrect_key', 'Incorrect Key: Private key is wrong/incorrect format.', 'key', 'rotate_key', 'client_lifetime', True),
    3: ('WrongType', 'wrong_type', 'Wrong type: Requesting an incorrect basic type.', 'request', 'fail', 'none', False),
    4: ('WrongFields', 'wrong_fields', 'Wrong fields: Requesting incorrect selection fields.', 'request', 'fail', 'none', False),
    5: ('TooManyRequests', 'too_many_requests', 'Too many requests: Requests are blocked for a small period of time because of too many requests per user (max 100 per minute).', 'rate', 'wait_rate_window', 'none', True),
    6: ('IncorrectId', 'incorrect_id', 'Incorrect ID: Wrong ID value.', 'request', 'fail', 'none', False),
    7: ('IncorrectIdEntityRelation', 'incorrect_id_entity_relation', 'Incorrect ID-entity relation: A requested selection is private (For example, personal data of another user / faction).', 'request', 'fail', 'none', False),
    8: ('IpBlocked', 'ip_blocked', 'IP block: Current IP is banned for a small period of time because of abuse.', 'ip', 'rotate_route', 'none', True),
    9: ('ApiDisabled', 'api_disabled', 'API disabled: Api system is currently disabled.', 'server', 'fail', 'none', False),
    10: ('KeyOwnerInFederalJail', 'key_owner_in_federal_jail', "Key owner is in federal jail: Current key can't be used because owner is in federal jail.", 'key', 'rotate_key', 'client_lifetime', True),
    11: ('KeyChangeCooldown', 'key_change_cooldown', 'Key change error: You can only change your API key once every 60 seconds.', 'request', 'fail', 'none', False),
    12: ('KeyReadError', 'key_read_error', 'Key read error: Error reading key from Database.', 'transient', 'retry_backoff', 'none', True),
    13: ('KeyTemporaryDisabled', 'key_temporary_disabled', "The key is temporarily disabled due to owner inactivity: The key owner hasn't been online for more than 7 days.", 'key', 'rotate_key', 'client_lifetime', True),
    14: ('DailyReadLimitReached', 'daily_read_limit_reached', 'Daily read limit reached: Too many records have been pulled today by this user from our cloud services.', 'key', 'rotate_key', 'until_utc_midnight', True),
    15: ('LogUnavailable', 'log_unavailable', 'Temporary error: An error code specifically for testing purposes that has no dedicated meaning.', 'request', 'fail', 'none', False),
    16: ('AccessLevelTooLow', 'access_level_too_low', 'Access level of this key is not high enough: Torn restricts selections based on the key access level.', 'access', 'rotate_key', 'none', True),
    17: ('BackendError', 'backend_error', 'Backend error occurred, please try again.', 'transient', 'retry_backoff', 'none', True),
    18: ('ApiKeyPaused', 'api_key_paused', 'API key has been paused by the owner.', 'key', 'rotate_key', 'client_lifetime', True),
    19: ('MustMigrateToCrimesV2', 'must_migrate_to_crimes_v2', 'Must be migrated to crimes 2.0.', 'request', 'fail', 'none', False),
    20: ('RaceNotFinished', 'race_not_finished', 'Race not yet finished.', 'request', 'fail', 'none', False),
    21: ('IncorrectCategory', 'incorrect_category', 'Incorrect category: Wrong cat value.', 'request', 'fail', 'none', False),
    22: ('OnlyAvailableInApiV1', 'only_available_in_api_v1', 'This selection is only available in API v1.', 'routing', 'reroute_version', 'none', True),
    23: ('OnlyAvailableInApiV2', 'only_available_in_api_v2', 'This selection is only available in API v2.', 'routing', 'reroute_version', 'none', True),
    24: ('ClosedTemporarily', 'closed_temporarily', 'Closed temporarily.', 'transient', 'retry_backoff', 'none', True),
    25: ('InvalidStatRequested', 'invalid_stat_requested', 'Invalid stat requested.', 'request', 'fail', 'none', False),
    26: ('OnlyCategoryOrStatsAllowed', 'only_category_or_stats_allowed', 'Only category or stats are allowed in a single request.', 'request', 'fail', 'none', False),
    27: ('MustMigrateToOrganizedCrimesV2', 'must_migrate_to_organized_crimes_v2', 'Must be migrated to organized crimes 2.0.', 'request', 'fail', 'none', False),
    28: ('IncorrectLogId', 'incorrect_log_id', 'Incorrect log ID.', 'request', 'fail', 'none', False),
    29: ('CategorySelectionUnavailableForInteractionLogs', 'category_selection_unavailable_for_interaction_logs', 'Category selection unavailable for interaction logs.', 'request', 'fail', 'none', False),
    30: ('FileDoesNotExist', 'file_does_not_exist', 'File does not exist.', 'request', 'fail', 'none', False),
    31: ('CityStatsCronFailed', 'city_stats_cron_failed', 'City stats cron failed.', 'transient', 'retry_backoff', 'none', True),
    32: ('EndpointClosedUntilAttackingPeriod', 'endpoint_closed_until_attacking_period', 'Endpoint closed until the attacking period.', 'request', 'fail', 'none', False),
    33: ('NoCompetitionAvailable', 'no_competition_available', 'No competition available.', 'request', 'fail', 'none', False),
}

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 30000
DEFAULT_RETRY_AFTER_CAP_MS = 120000
HTTP_RETRY_STATUSES = frozenset((429, 500, 502, 503, 504))
