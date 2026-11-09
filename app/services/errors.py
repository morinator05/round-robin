"""The errors services raise when a rule says no. Their message is shown to the user as is."""


class ServiceError(Exception):
    """An action isn't allowed or the input is invalid."""


class DutyError(ServiceError):
    """An action on a duty isn't allowed."""


class UserError(ServiceError):
    """Invalid input about a resident, or a wrong password."""


class DishwasherError(ServiceError):
    """An action on the dishwasher list isn't allowed."""
