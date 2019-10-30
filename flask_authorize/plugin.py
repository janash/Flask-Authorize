# -*- coding: utf-8 -*-
#
# Plugin Setup
#
# ------------------------------------------------


# imports
# -------
import six
import types
from functools import wraps
from flask import current_app
from werkzeug.exceptions import Unauthorized

from .mixins import default_permissions, default_allowances, table_key


# constants
# ---------
AUTHORIZE_CACHE = dict()
CURRENT_USER = None


# helpers
# -------
def flask_login_current_user():
    try:
        from flask_login import current_user
        user = current_user
    except ImportError:
        raise AssertionError(
            'Error: Flask-Authorize requires that either '
            'Flask-Login is used or that `user` is '
            'specified to authorization method')
    return user


def has_permission(expected, actual):
    """
    Check if singular set of expected/actual
    permissions are appropriate.
    """
    x = set(expected).intersection(actual)
    return len(x) == len(expected)


# plugin
# ------
class Authorize(object):
    """
    Plugin for updating flask functions to handle class-based URL
    routing.
    """

    def __init__(self, app=None, current_user=flask_login_current_user):
        if app is not None:
            self.init_app(app, current_user=current_user)

        return

    def init_app(self, app, current_user=None):
        # settings
        app.config.setdefault('AUTHORIZE_DEFAULT_PERMISSIONS', dict(
            owner=['read', 'update', 'delete'],
            group=['read', 'update'],
            other=['read']
        ))
        app.config.setdefault('AUTHORIZE_DEFAULT_RESTRICTIONS', [])
        app.config.setdefault('AUTHORIZE_DEFAULT_ALLOWANCES', ['read', 'update', 'delete'])
        app.config.setdefault('AUTHORIZE_MODEL_PARSER', 'table')

        self.app = app

        # set current user function
        if current_user is not None:
            if not callable(current_user):
                raise AssertionError('Error: `current_user` input must be callable.')
            global CURRENT_USER
            CURRENT_USER = current_user
        return

    @property
    def delete(self):
        return Authorizer(permission='delete')

    @property
    def read(self):
        return Authorizer(permission='read')

    @property
    def update(self):
        return Authorizer(permission='update')

    def create(self, model):
        return Authorizer(permission='create', model=model)

    ## TODO: FIGURE OUT CUSTOM SCHEMES 

    def has_role(self, role):
        return Authorizer(has_role=role)

    def in_group(self, group):
        return Authorizer(in_group=group)


# helpers
# -------
def user_has_role(user, roles):
    """
    Check if specified user has one of the specified roles.
    """
    if not hasattr(user, 'roles'):
        return False
    for role in user.roles:
        check = role.name if hasattr(role, 'name') else str(role)
        if check in roles:
            return True
    return False


def user_in_group(user, groups):
    """
    Check if specified user is in one of the specified groups.
    """
    if not hasattr(user, 'groups'):
        return False
    for group in user.groups:
        check = group.name if hasattr(group, 'name') else str(group)
        if check in groups:
            return True
    return False


def user_is_restricted(user, operation, obj):
    key = table_key(obj.__class__)

    # gather credentials to check
    credentials = []
    if hasattr(user, 'roles'):
        credentials.extend(user.roles)
    if hasattr(user, 'groups'):
        credentials.extend(user.groups)
    if not len(credentials):
        return False

    # check all credentials
    for cred in credentials:
        if hasattr(cred, 'restrictions') and cred.restrictions is not None:
            check = set(cred.restrictions.get(key, []))
            if len(check.intersection(operation)):
                return True
    return False


def user_is_allowed(user, operation, obj):
    key = table_key(obj.__class__)

    # gather credentials to check
    credentials = []
    if hasattr(user, 'roles'):
        credentials.extend(user.roles)
    if hasattr(user, 'groups'):
        credentials.extend(user.groups)
    if not len(credentials):
        return True

    # gather allowances from credentials
    allowances, default = [], default_allowances()
    for cred in credentials:

        # if not restricting allowances on one
        # of the credentials, it's allowed
        if not hasattr(cred, 'allowances'):
            return True
        if cred.allowances is None:
            return True

        allowances.extend(cred.allowances.get(key, default))

    # check allowances
    check = set(allowances).intersection(operation)
    if len(check) == len(operation):
        return True

    return False


# processor
# ---------
class Authorizer(object):
    """
    Decorator for authorizing the ability of the current
    user to perform actions on various models.

    .. code-block:: python

        @app.route('/profile', method=['GET'])
        @authorize.self(User.current)
        def get_profile():
            return

        @app.route('/users/<id(User):user>', method=['GET'])
        @authorize.read
        def get_user(user):
            return

        @app.route('/users/<id(User):user>', method=['PUT'])
        @authorize.update
        @authorize.role('user-updators')
        def update_user(user):
            return

        @app.route('/users/<ident>', method=['PUT'])
        def update_user(ident):
            user = User.get(id=ident)
            if not authorize.update(user) or not authorize.role('test-role'):
                raise Unauthorized
            return


    """

    def __init__(self, permission=None, has_role=None, in_group=None, model=None):
        def _(arg):
            if arg is None:
                arg = []
            if not isinstance(arg, (list, tuple)):
                arg = [arg]
            return arg

        self.permission = _(permission)
        self.has_role = _(has_role)
        self.in_group = _(in_group)
        self.model = _(model)
        return

    def __call__(self, *cargs, **ckwargs):

        # dispatch on whether or not being used as decorator
        if not len(cargs):
            raise AssertionError('Authorizer needs to be passed function for decoration or objects to authorize.')
        if not isinstance(cargs[0], types.FunctionType):
            return self.allowed(*cargs, user=ckwargs.get('user'))

        # allow for duplicate decorations on functions
        func = cargs[0]
        if func.__name__ not in AUTHORIZE_CACHE:
            AUTHORIZE_CACHE[func.__name__] = self
        else:
            original = AUTHORIZE_CACHE[func.__name__]
            updated = Authorizer(
                permission=original.permission + self.permission,
                has_role=original.has_role + self.has_role,
                in_group=original.in_group + self.in_group,
                model=original.model + self.model
            )
            AUTHORIZE_CACHE[func.__name__] = updated
            del original

        auth = AUTHORIZE_CACHE[func.__name__]

        @wraps(func)
        def inner(*args, **kwargs):
            # gather all items to check authorization for
            check = list(args) + list(kwargs.values())

            # check if authorized
            if not auth.allowed(*check):
                raise Unauthorized

            return func(*args, **kwargs)
        return inner

    def allowed(self, *args, **kwargs):

        # look to flask-login for current user
        user = kwargs.get('user')
        if user is None:
            user = CURRENT_USER()

        # otherwise, use current user method
        elif isinstance(user, types.FunctionType):
            user = user()

        # don't allow anything for anonymous users
        if user is None:
            return False

        # authorize if user has relevant role
        if len(self.has_role):
            if user_has_role(user, self.has_role):
                return True

        # authorize if user has relevant group
        if len(self.in_group):
            if user_in_group(user, self.in_group):
                return True

        # return if no additional permission check needed
        if len(self.permission) == 0:
            return False

        # check permissions on individual instances
        operation = set(self.permission)
        for arg in args:

            # only check permissions for items that have set permissions
            if not isinstance(arg.__class__, six.class_types):
                continue
            if not hasattr(arg, 'permissions'):
                continue

            # # check role restrictions/allowances
            if user_is_restricted(user, operation, arg):
                return False

            if not user_is_allowed(user, operation, arg):
                return False

            # check other permissions
            check = arg.permissions.get('other', {})
            if has_permission(operation, check):
                return True

            # check user permissions
            if hasattr(arg, 'owner'):
                if arg.owner == user:
                    check = arg.permissions.get('owner', {})
                    if has_permission(operation, check):
                        return True

            # check group permissions
            if hasattr(arg, 'group'):
                if hasattr(user, 'groups'):
                    if arg.group in user.groups:
                        check = arg.permissions.get('group', {})
                        if has_permission(operation, check):
                            return True

        return False
