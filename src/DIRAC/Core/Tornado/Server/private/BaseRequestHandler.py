"""DIRAC server has various passive components listening to incoming client requests and reacting accordingly by serving requested information, such as **services** or **APIs**. This module is basic for each of these components and describes the basic concept of access to them.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

__RCSID__ = "$Id$"

import time
import inspect
import threading
from datetime import datetime

from http import HTTPStatus
from urllib.parse import unquote
from functools import partial

import jwt
import tornado
from tornado import gen
from tornado.web import RequestHandler, HTTPError
from tornado.ioloop import IOLoop
from tornado.concurrent import Future

import DIRAC

from DIRAC import gConfig, gLogger, S_OK, S_ERROR
from DIRAC.Core.Utilities import DErrno
from DIRAC.Core.DISET.AuthManager import AuthManager
from DIRAC.Core.Utilities.JEncode import decode, encode
from DIRAC.Core.Utilities.ReturnValues import isReturnStructure
from DIRAC.Core.Security.X509Chain import X509Chain  # pylint: disable=import-error
from DIRAC.FrameworkSystem.Client.MonitoringClient import MonitoringClient
from DIRAC.Resources.IdProvider.Utilities import getProvidersForInstance
from DIRAC.Resources.IdProvider.IdProviderFactory import IdProviderFactory

sLog = gLogger.getSubLogger(__name__.split(".")[-1])


class TornadoResponse:
    """:py:class:`~BaseRequestHandler` uses multithreading to process requests, so the logic you describe in the target method
    in the handler that inherit ``BaseRequestHandler`` will be called in a non-main thread.

    Tornado warns that "methods on RequestHandler and elsewhere in Tornado are not thread-safe"
    https://www.tornadoweb.org/en/stable/web.html#thread-safety-notes.

    This class registers tornado methods with arguments in the same order they are called
    from ``TornadoResponse`` instance to call them later in the main thread and can be useful
    if you are afraid to use Tornado methods in a non-main thread due to a warning from Tornado.

    This is used in exceptional cases, in most cases it is not required, just use ``return S_OK(data)`` instead.

    Usage example::

        class MyHandler(BaseRequestHandlerChildHandler):

            def export_myTargetMethod(self):
                # Here we want to use the tornado method, but we want to do it in the main thread.
                # Let's create an TornadoResponse instance and
                # call the tornado methods we need in the order in which we want them to run in the main thread.
                resp = TornadoResponse('data')
                resp.set_header("Content-Type", "application/x-tar")
                # And finally, for example, redirect to another place
                return resp.redirect('https://my_another_server/redirect_endpoint')

    """

    # Let's see what methods RequestHandler has
    __attrs = inspect.getmembers(RequestHandler)

    def __init__(self, payload=None, status_code=None):
        """C'or

        :param payload: response body
        :param int status_code: response status code
        """
        self.payload = payload
        self.status_code = status_code
        self.actions = []  # a list of registered actions to perform in the main thread
        for mName, mObj in self.__attrs:
            # Let's make sure that this is the usual RequestHandler method
            if inspect.isroutine(mObj) and not mName.startswith("_") and not mName.startswith("get"):
                setattr(self, mName, partial(self.__setAction, mName))

    def __setAction(self, methodName, *args, **kwargs):
        """Register new action

        :param str methodName: ``RequestHandler`` method name

        :return: ``TornadoResponse`` instance
        """
        self.actions.append((methodName, args, kwargs))
        # Let's return the instance of the class so that it can be returned immediately. For example:
        # resp = TornadoResponse('data')
        # return resp.redirect('https://server')
        return self

    def _runActions(self, reqObj):
        """This method is executed after returning to the main thread.
        Look the :py:meth:`__finishFuture` method.

        :param reqObj: ``RequestHandler`` instance
        """
        # Assign a status code if it has been transmitted.
        if self.status_code:
            reqObj.set_status(self.status_code)
        for mName, args, kwargs in self.actions:
            getattr(reqObj, mName)(*args, **kwargs)
        # Will we check if the finish method has already been called.
        if not reqObj._finished:
            # if not what are we waiting for?
            reqObj.finish(self.payload)


class BaseRequestHandler(RequestHandler):
    """This class primarily describes the process of processing an incoming request and the methods of authentication and authorization.

    Each HTTP request is served by a new instance of this class.

    For the sequence of method called, please refer to
    the `tornado documentation <https://www.tornadoweb.org/en/stable/guide/structure.html>`_.

    In order to pass information around and keep some states, we use instance attributes.
    These are initialized in the :py:meth:`.initialize` method.

    This class is basic for :py:class:`TornadoService <DIRAC.Core.Tornado.Server.TornadoService.TornadoService>`
    and :py:class:`TornadoREST <DIRAC.Core.Tornado.Server.TornadoREST.TornadoREST>`.
    Check them out, this is a good example of writing a new child class if needed.

    .. digraph:: structure
        :align: center

        node [shape=plaintext]
        RequestHandler [label="tornado.web.RequestHandler"];

        {TornadoService, TornadoREST} -> BaseRequestHandler;
        BaseRequestHandler -> RequestHandler [label="  inherit", fontsize=8];

    In order to create a class that inherits from ``BaseRequestHandler``, first you need to determine what HTTP methods need to be supported.
    Override the class variable ``SUPPORTED_METHODS`` by writing down the necessary methods there.
    Note that by default all HTTP methods are supported.

    It is important to understand that the handler belongs to the system.
    The class variable ``SYSTEM_NAME`` displays the system name. By default it is taken from the module name.
    This value is used to generate the full component name, see :py:meth:`_getFullComponentName` method

    This class also defines some variables for writing your handler's methods:

        - ``DEFAULT_AUTHORIZATION`` describes the general authorization rules for the entire handler
        - ``auth_<method name>`` describes authorization rules for a single method and has higher priority than ``DEFAULT_AUTHORIZATION``
        - ``METHOD_PREFIX`` helps in finding the target method, see the :py:meth:`__getMethod` methods, where described how exactly.

    It is worth noting that DIRAC supports several ways to authorize the request and they are all descriptive in ``USE_AUTHZ_GRANTS``.
    Grant name is associated with ``_authz<GRANT NAME>`` method and will be applied alternately as they are defined in the variable until one of them is successfully executed.
    If no authorization method completes successfully, access will be denied.
    The following authorization methods are supported by default:

        - ``SSL`` (:py:meth:`_authzSSL`) - reads the X509 certificate sent with the request
        - ``JWT`` (:py:meth:`_authzJWT`) - reads the Bearer Access Token sent with the request
        - ``VISITOR`` (:py:meth:`_authzVISITOR`) - authentication as visitor

    Also, if necessary, you can create a new type of authorization by simply creating the appropriate method::

        def _authzMYAUTH(self):
            '''Another authorization algoritm.'''
            # Do somthing
            return S_OK(credentials)  # return user credentials as a dictionary

    The name of the component to monitor the developer can specify in the ``MONITORING_COMPONENT`` class variable,
    see :py:class:`MonitoringClient <DIRAC.FrameworkSystem.Client.MonitoringClient.MonitoringClient>` class for more details.

    Review the class variables, explanatory comments. You are free to overwrite class variables to suit your needs.

    The class contains methods that require implementation:

        - :py:meth:`_getCSAuthorizarionSection`
        - :py:meth:`_getMethodName`
        - :py:meth:`_getMethodArgs`

    Some methods have basic behavior, but developers can rewrite them:

        - :py:meth:`_getFullComponentName`
        - :py:meth:`_getComponentInfoDict`
        - :py:meth:`_initializeHandler`
        - :py:meth:`_monitorRequest`
        - :py:meth:`_getMethodAuthProps`

    Designed for overwriting in the final handler if necessary:

        - :py:meth:`initializeHandler`
        - :py:meth:`initializeRequest`

    .. warning:: Do not change methods derived from ``tornado.web.RequestHandler``, e.g.: initialize, prepare.

    Let's analyze the incoming request processing algorithm.
    But before the handler can accept requests, you need to start :py:mod:`TornadoServer <DIRAC.Core.Tornado.Server.TornadoServer>`.
    At startup, :py:class:`HandlerManager <DIRAC.Core.Tornado.Server.HandlerManager.HandlerManager>` inspects the handler
    and its methods to generate tornados of access URLs to it.

    The first request starts the process of initializing the handler, see the :py:meth:`initialize` method:

        - specifies the full name of the component, including the name of the system to which it belongs, see :py:meth:`_getFullComponentName`.
        - initialization of the main authorization class, see :py:class:`AuthManager <DIRAC.Core.DISET.AuthManager.AuthManager>` for more details.
        - initialization of the monitoring specific to this handler, see :py:meth:`_initMonitoring`.
        - initialization of the target handler that inherit this one, see :py:meth:`initializeHandler` and :py:meth:`_initializeHandler`.
        - load all registered identity providers for authentication with access token

    Next, first of all the tornados prepare method is called which does the following:

        - determines determines the name of the target method and checks its presence, see :py:meth:`_getMethodName` and :py:meth:`__getMethod`.
        - request monitoring, see :py:meth:`_monitorRequest`.
        - authentication request using one of the available algorithms called ``USE_AUTHZ_GRANTS``, see :py:meth:`_gatherPeerCredentials` for more details.
        - and finally authorizing the request to access the component, see :py:meth:`authQuery <DIRAC.Core.DISET.AuthManager.AuthManager.authQuery>` for more details.

    If all goes well, then a method is executed, the name of which coincides with the name of the request method (e.g.: :py:meth:`get`) which does:

        - defines the arguments of the target method, see :py:meth:`_getMethodArgs`.
        - execute the target method in an executor a separate thread.
        - the result of the target method is processed in the main thread and returned to the client, see :py:meth:`__finishFuture`.

    """

    # Because we initialize at first request, we use a flag to know if it's already done
    __init_done = False
    # Lock to make sure that two threads are not initializing at the same time
    __init_lock = threading.RLock()

    # MonitoringClient, we don't use gMonitor which is not thread-safe
    # We also need to add specific attributes for each service
    # See _initMonitoring method for the details.
    _monitor = None

    # Definition of identity providers, used to authorize requests with access tokens
    _idps = IdProviderFactory()
    _idp = {}

    # The variable that will contain the result of the request, see __finishFuture method
    __result = None

    # Below are variables that the developer can OVERWRITE as needed

    # System name with which this component is associated.
    # Developer can overwrite this if your handler is outside the DIRAC system package (src/DIRAC/XXXSystem/<path to your handler>)
    SYSTEM_NAME = None

    # Authorization requirements, properties that applied by default to all handler methods, if defined.
    # Note that `auth_methodName` will have a higher priority.
    DEFAULT_AUTHORIZATION = None

    # Type of component, see MonitoringClient class
    MONITORING_COMPONENT = MonitoringClient.COMPONENT_WEB

    # Prefix of the target methods names if need to use a special prefix. By default its "export_".
    METHOD_PREFIX = "export_"

    # What grant type to use. This definition refers to the type of authentication, ie which algorithm will be used to verify the incoming request and obtain user credentials.
    # These algorithms will be applied in the same order as in the list.
    #  SSL - add to list to enable certificate reading
    #  JWT - add to list to enable reading Bearer token
    #  VISITOR - add to list to enable authentication as visitor, that is, without verification
    USE_AUTHZ_GRANTS = ["SSL", "JWT"]

    @classmethod
    def _initMonitoring(cls, fullComponentName: str, fullUrl: str):
        """
        Initialize the monitoring specific to this handler
        This has to be called only by :py:meth:`.__initialize`
        to ensure thread safety and unicity of the call.

        :param componentName: relative URL ``/<System>/<Component>``
        :param fullUrl: full URl like ``https://<host>:<port>/<System>/<Component>``
        """

        # Init extra bits of monitoring

        cls._monitor = MonitoringClient()
        cls._monitor.setComponentType(cls.MONITORING_COMPONENT)

        cls._monitor.initialize()

        if tornado.process.task_id() is None:  # Single process mode
            cls._monitor.setComponentName("Tornado/%s" % fullComponentName)
        else:
            cls._monitor.setComponentName("Tornado/CPU%d/%s" % (tornado.process.task_id(), fullComponentName))

        cls._monitor.setComponentLocation(fullUrl)

        cls._monitor.registerActivity("Queries", "Queries served", "Framework", "queries", MonitoringClient.OP_RATE)

        cls._monitor.setComponentExtraParam("DIRACVersion", DIRAC.version)
        cls._monitor.setComponentExtraParam("platform", DIRAC.getPlatform())
        cls._monitor.setComponentExtraParam("startTime", datetime.utcnow())

        cls._stats = {"requests": 0, "monitorLastStatsUpdate": time.time()}

        return S_OK()

    @classmethod
    def _getFullComponentName(cls) -> str:
        """Search the full name of the component, including the name of the system to which it belongs.
        CAN be implemented by developer.
        """
        handlerName = cls.__name__[: -len("Handler")]
        return f"{cls.SYSTEM_NAME}/{handlerName}" if cls.SYSTEM_NAME else handlerName

    @classmethod
    def _getCSAuthorizarionSection(cls, fullComponentName: str) -> str:
        """Search component authorization section in CS.
        SHOULD be implemented by developer.

        :param fullComponentName: full component name, see :py:meth:`_getFullComponentName`
        """
        raise NotImplementedError("Please, create the _getCSAuthorizarionSection class method")

    @classmethod
    def _getComponentInfoDict(cls, fullComponentName: str, fullURL: str) -> dict:
        """Fills the dictionary with information about the current component,
        e.g.: 'serviceName', 'serviceSectionPath', 'csPaths'.
        SHOULD be implemented by developer.

        :param fullComponentName: full component name, see :py:meth:`_getFullComponentName`
        :param fullURL: incoming request path
        """
        raise NotImplementedError("Please, create the _getComponentInfoDict class method")

    @classmethod
    def __loadIdPs(cls):
        """Load identity providers that will be used to verify tokens"""
        sLog.info("Load identity providers..")
        # Research Identity Providers
        result = getProvidersForInstance("Id")
        if result["OK"]:
            for providerName in result["Value"]:
                result = cls._idps.getIdProvider(providerName)
                if result["OK"]:
                    cls._idp[result["Value"].issuer.strip("/")] = result["Value"]
                else:
                    sLog.error(result["Message"])

    @classmethod
    def __initialize(cls, request):
        """
        Initialize a component.
        The work is only performed once at the first request.

        :param object request: incoming request, :py:class:`tornado.httputil.HTTPServerRequest`

        :returns: S_OK
        """
        # If the initialization was already done successfuly,
        # we can just return
        if cls.__init_done:
            return S_OK()

        # Otherwise, do the work but with a lock
        with cls.__init_lock:

            # Check again that the initialization was not done by another thread
            # while we were waiting for the lock
            if cls.__init_done:
                return S_OK()

            if cls.SYSTEM_NAME is None:
                # If the system name is not specified, it is taken from the module.
                cls.SYSTEM_NAME = ([m[:-6] for m in cls.__module__.split(".") if m.endswith("System")] or [None]).pop()

            # absoluteUrl: full URL e.g. ``https://<host>:<port>/<System>/<Component>``
            absoluteUrl = request.path
            # Set full component name, e.g.: <System>/<Component>
            cls._fullComponentName = cls._getFullComponentName()

            # The time at which the handler was initialized
            cls._startTime = datetime.utcnow()
            sLog.info(f"First use of {cls._fullComponentName}, initializing..")

            # authorization manager initialization
            cls._authManager = AuthManager(cls._getCSAuthorizarionSection(cls._fullComponentName))

            # component monitoring initialization
            cls._initMonitoring(cls._fullComponentName, absoluteUrl)

            cls._componentInfoDict = cls._getComponentInfoDict(cls._fullComponentName, absoluteUrl)

            # Some pre-initialization
            cls._initializeHandler()

            cls.initializeHandler(cls._componentInfoDict)

            # Load all registered identity providers
            cls.__loadIdPs()

            cls.__init_done = True

            return S_OK()

    @classmethod
    def _initializeHandler(cls):
        """If you are writing your own framework that inherit this class
        and you need to pre-initialize something before :py:meth:`initializeHandler`,
        such as initializing the OAuth client, then you need to change this method.
        CAN be implemented by developer.
        """
        pass

    @classmethod
    def initializeHandler(cls, componentInfo: dict):
        """This method for handler initializaion. This method is called only one time,
        at the first request. CAN be implemented by developer.

        :param componentInfo: infos about component, see :py:meth:`_getComponentInfoDict`.
        """
        pass

    def initializeRequest(self):
        """Called at every request, may be overwritten in your handler.
        CAN be implemented by developer.
        """
        pass

    # This is a Tornado magic method
    def initialize(self):  # pylint: disable=arguments-differ
        """
        Initialize the handler, called at every request.

        It just calls :py:meth:`.__initialize`

        If anything goes wrong, the client will get ``Connection aborted``
        error. See details inside the method.

        ..warning::
          DO NOT REWRITE THIS FUNCTION IN YOUR HANDLER
          ==> initialize in DISET became initializeRequest in HTTPS !
        """
        # Only initialized once
        if not self.__init_done:
            # Ideally, if something goes wrong, we would like to return a Server Error 500
            # but this method cannot write back to the client as per the
            # `tornado doc <https://www.tornadoweb.org/en/stable/guide/structure.html#overriding-requesthandler-methods>`_.
            # So the client will get a ``Connection aborted```
            try:
                res = self.__initialize(self.request)
                if not res["OK"]:
                    raise Exception(res["Message"])
            except Exception as e:
                sLog.error("Error in initialization", repr(e))
                raise

    def _monitorRequest(self):
        """Monitor action for each request.
        CAN be implemented by developer.
        """
        self._monitor.setComponentLocation(self.request.path)
        self._stats["requests"] += 1
        self._monitor.setComponentExtraParam("queries", self._stats["requests"])
        self._monitor.addMark("Queries")

    def _getMethodName(self) -> str:
        """Parse method name from incoming request.
        Based on this name, the target method to run will be determined.
        SHOULD be implemented by developer.
        """
        raise NotImplementedError("Please, create the _getMethodName method")

    def _getMethodArgs(self, args: tuple) -> tuple:
        """Decode target method arguments from incoming request.
        SHOULD be implemented by developer.

        :param args: arguments comming to :py:meth:`get` and other HTTP methods.

        :return: (list, dict) -- tuple contain args and kwargs
        """
        raise NotImplementedError("Please, create the _getMethodArgs method")

    def _getMethodAuthProps(self) -> list:
        """Resolves the hard coded authorization requirements for the method.
        CAN be implemented by developer.

        List of required :mod:`Properties <DIRAC.Core.Security.Properties>`.
        """
        # Convert default authorization requirements to list
        if self.DEFAULT_AUTHORIZATION and not isinstance(self.DEFAULT_AUTHORIZATION, (list, tuple)):
            self.DEFAULT_AUTHORIZATION = [p.strip() for p in self.DEFAULT_AUTHORIZATION.split(",") if p.strip()]
        # Use auth_< method name > as primary value of the authorization requirements
        return getattr(self, "auth_" + self.methodName, self.DEFAULT_AUTHORIZATION)

    def __getMethod(self):
        """Get target method function to call.

        :return: function
        """
        # Get method object using prefix and method name from request
        methodObj = getattr(self, f"{self.METHOD_PREFIX}{self.methodName}", None)
        if not callable(methodObj):
            sLog.error("Invalid method", self.methodName)
            raise HTTPError(status_code=HTTPStatus.NOT_IMPLEMENTED)
        return methodObj

    def prepare(self):
        """Tornados prepare method that called before request"""
        # Define the target method
        self.methodName = self._getMethodName()
        self.methodObj = self.__getMethod()
        # Register activities
        self._monitorRequest()

        self._prepare()

    def _prepare(self):
        """Prepare the request. It reads certificates or tokens and check authorizations.
        We make the assumption that there is always going to be a ``method`` argument
        regardless of the HTTP method used
        """
        try:
            self.credDict = self._gatherPeerCredentials()
        except Exception as e:  # pylint: disable=broad-except
            # If an error occur when reading certificates we close connection
            # It can be strange but the RFC, for HTTP, say's that when error happend
            # before authentication we return 401 UNAUTHORIZED instead of 403 FORBIDDEN
            sLog.debug(str(e))
            sLog.error("Error gathering credentials ", "%s; path %s" % (self.getRemoteAddress(), self.request.path))
            raise HTTPError(HTTPStatus.UNAUTHORIZED, str(e))

        # Check whether we are authorized to perform the query
        # Note that performing the authQuery modifies the credDict...
        authorized = self._authManager.authQuery(self.methodName, self.credDict, self._getMethodAuthProps())
        if not authorized:
            extraInfo = ""
            if self.credDict.get("ID"):
                extraInfo += "ID: %s" % self.credDict["ID"]
            elif self.credDict.get("DN"):
                extraInfo += "DN: %s" % self.credDict["DN"]
            sLog.error(
                "Unauthorized access",
                "Identity %s; path %s; %s" % (self.srv_getFormattedRemoteCredentials(), self.request.path, extraInfo),
            )
            raise HTTPError(HTTPStatus.UNAUTHORIZED)

    def __executeMethod(self, targetMethod: str, args: list, kwargs: dict):
        """
        Execute the method called, this method is ran in an executor
        We have several try except to catch the different problem which can occur

        - First, the method does not exist => Attribute error, return an error to client
        - second, anything happend during execution => General Exception, send error to client

        .. warning:: This method is called in an executor, and so cannot use methods like self.write, see :py:class:`TornadoResponse`.

        :param targetMethod: name of the method to call
        :param args: target method arguments
        :param kwargs: target method keyword arguments
        """

        sLog.notice(
            "Incoming request %s /%s: %s"
            % (self.srv_getFormattedRemoteCredentials(), self._fullComponentName, self.methodName)
        )
        # Execute
        try:
            self.initializeRequest()
            return targetMethod(*args, **kwargs)
        except Exception as e:  # pylint: disable=broad-except
            sLog.exception("Exception serving request", "%s:%s" % (str(e), repr(e)))
            raise e if isinstance(e, HTTPError) else HTTPError(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def __prepareExecutor(self, args: list):
        """Preparation of necessary arguments for the :py:meth:`__executeMethod` method

        :param args: arguments passed to the ``post`, ``get``, etc. tornado methods

        :return: executor, target method with arguments
        """
        args, kwargs = self._getMethodArgs(args)
        return None, partial(self.__executeMethod, self.methodObj, args, kwargs)

    def __finishFuture(self, retVal):
        """Handler Future result

        :param object retVal: tornado.concurrent.Future
        """
        # Wait result only if it's a Future object
        self.__result = retVal.result() if isinstance(retVal, Future) else retVal

        # Strip the exception/callstack info from S_ERROR responses
        if isinstance(self.__result, dict):
            # ExecInfo comes from the exception
            if "ExecInfo" in self.__result:
                del self.__result["ExecInfo"]
            # CallStack comes from the S_ERROR construction
            if "CallStack" in self.__result:
                del self.__result["CallStack"]

        # Here it is safe to write back to the client, because we are not in a thread anymore

        # If you need to end the method using tornado methods, outside the thread,
        # you need to define the finish_<methodName> method.
        # This method will be started after __executeMethod is completed.
        finishFunc = getattr(self, "finish_%s" % self.methodName, None)

        if isinstance(self.__result, TornadoResponse):
            self.__result._runActions(self)

        elif callable(finishFunc):
            finishFunc()

        # In case nothing is returned
        elif self.__result is None:
            self.finish()

        # If set to true, do not JEncode the return of the RPC call
        # This is basically only used for file download through
        # the 'streamToClient' method.
        elif self.get_argument("rawContent", default=False):
            # See 4.5.1 http://www.rfc-editor.org/rfc/rfc2046.txt
            self.set_header("Content-Type", "application/octet-stream")
            self.finish(self.__result)

        # Return simple text or html
        elif isinstance(self.__result, str):
            self.finish(self.__result)

        # JSON
        else:
            self.set_header("Content-Type", "application/json")
            self.finish(encode(self.__result))

    def on_finish(self):
        """
        Called after the end of HTTP request.
        Log the request duration
        """
        elapsedTime = 1000.0 * self.request.request_time()
        credentials = self.srv_getFormattedRemoteCredentials()

        argsString = f"OK {self._status_code}"
        # Finish with DIRAC result
        if isReturnStructure(self.__result):
            argsString = "OK" if self.__result["OK"] else f"ERROR: {self.__result['Message']}"
        # If bad HTTP status code
        if self._status_code >= 400:
            argsString = f"ERROR {self._status_code}: {self._reason}"

        sLog.notice(
            "Returning response", f"{credentials} {self._fullComponentName} ({elapsedTime:.2f} ms) {argsString}"
        )

    def _gatherPeerCredentials(self, grants: list = None) -> dict:
        """Returne a dictionary designed to work with the :py:class:`AuthManager <DIRAC.Core.DISET.AuthManager.AuthManager>`,
        already written for DISET and re-used for HTTPS.

        This method attempts to authenticate the request by using the authentication types defined in ``USE_AUTHZ_GRANTS``.

        The following types of authentication are currently available:

          - certificate reading, see :py:meth:`_authzSSL`.
          - reading Bearer token, see :py:meth`_authzJWT`.
          - authentication as visitor, that is, without verification, see :py:meth`_authzVISITOR`.

        To add your own authentication type, create a `_authzYourGrantType` method that should return ``S_OK(dict)`` in case of successful authorization.

        :param grants: grants to use

        :returns: a dict containing user credentials
        """
        err = []

        # At least some authorization method must be defined, if nothing is defined,
        # the authorization will go through the `_authzVISITOR` method and
        # everyone will have access as anonymous@visitor
        for grant in grants or self.USE_AUTHZ_GRANTS or "VISITOR":
            grant = grant.upper()
            grantFunc = getattr(self, "_authz%s" % grant, None)
            # pylint: disable=not-callable
            result = grantFunc() if callable(grantFunc) else S_ERROR("%s authentication type is not supported." % grant)
            if result["OK"]:
                for e in err:
                    sLog.debug(e)
                sLog.debug("%s authentication success." % grant)
                return result["Value"]
            err.append("%s authentication: %s" % (grant, result["Message"]))

        # Report on failed authentication attempts
        raise Exception("; ".join(err))

    def _authzSSL(self):
        """Load client certchain in DIRAC and extract informations.

        :return: S_OK(dict)/S_ERROR()
        """
        try:
            derCert = self.request.get_ssl_certificate()
        except Exception:
            # If 'IOStream' object has no attribute 'get_ssl_certificate'
            derCert = None

        # Get client certificate as pem
        if derCert:
            chainAsText = derCert.as_pem().decode()
            # Read all certificate chain
            chainAsText += "".join([cert.as_pem().decode() for cert in self.request.get_ssl_certificate_chain()])
        elif self.request.headers.get("X-Ssl_client_verify") == "SUCCESS" and self.request.headers.get("X-SSL-CERT"):
            chainAsText = unquote(self.request.headers.get("X-SSL-CERT"))
        else:
            return S_ERROR(DErrno.ECERTFIND, "Valid certificate not found.")

        # Load full certificate chain
        peerChain = X509Chain()
        peerChain.loadChainFromString(chainAsText)

        # Retrieve the credentials
        res = peerChain.getCredentials(withRegistryInfo=False)
        if not res["OK"]:
            return res

        credDict = res["Value"]

        # We check if client sends extra credentials...
        if "extraCredentials" in self.request.arguments:
            extraCred = self.get_argument("extraCredentials")
            if extraCred:
                credDict["extraCredentials"] = decode(extraCred)[0]
        return S_OK(credDict)

    def _authzJWT(self, accessToken=None):
        """Load token claims in DIRAC and extract informations.

        :param str accessToken: access_token

        :return: S_OK(dict)/S_ERROR()
        """
        if not accessToken:
            # Export token from headers
            token = self.request.headers.get("Authorization")
            if not token or len(token.split()) != 2:
                return S_ERROR(DErrno.EATOKENFIND, "Not found a bearer access token.")
            tokenType, accessToken = token.split()
            if tokenType.lower() != "bearer":
                return S_ERROR(DErrno.ETOKENTYPE, "Found a not bearer access token.")

        # Read token without verification to get issuer
        self.log.debug("Read issuer from access token", accessToken)
        issuer = jwt.decode(accessToken, leeway=300, options=dict(verify_signature=False, verify_aud=False))[
            "iss"
        ].strip("/")
        # Verify token
        self.log.debug("Verify access token")
        result = self._idp[issuer].verifyToken(accessToken)
        self.log.debug("Search user group")
        return self._idp[issuer].researchGroup(result["Value"], accessToken) if result["OK"] else result

    def _authzVISITOR(self):
        """Visitor access

        :return: S_OK(dict)
        """
        return S_OK({})

    @property
    def log(self):
        return sLog

    def getUserDN(self):
        return self.credDict.get("DN", "")

    def getUserName(self):
        return self.credDict.get("username", "")

    def getUserGroup(self):
        return self.credDict.get("group", "")

    def getProperties(self):
        return self.credDict.get("properties", [])

    def isRegisteredUser(self):
        return self.credDict.get("username", "anonymous") != "anonymous" and self.credDict.get("group")

    @classmethod
    def srv_getCSOption(cls, optionName, defaultValue=False):
        """
        Get an option from the CS section of the services

        :return: Value for serviceSection/optionName in the CS being defaultValue the default
        """
        if optionName[0] == "/":
            return gConfig.getValue(optionName, defaultValue)
        for csPath in cls._componentInfoDict["csPaths"]:
            result = gConfig.getOption(
                "%s/%s"
                % (
                    csPath,
                    optionName,
                ),
                defaultValue,
            )
            if result["OK"]:
                return result["Value"]
        return defaultValue

    def getCSOption(self, optionName, defaultValue=False):
        """
        Just for keeping same public interface
        """
        return self.srv_getCSOption(optionName, defaultValue)

    def srv_getRemoteAddress(self):
        """
        Get the address of the remote peer.

        :return: Address of remote peer.
        """

        remote_ip = self.request.remote_ip
        # Although it would be trivial to add this attribute in _HTTPRequestContext,
        # Tornado won't release anymore 5.1 series, so go the hacky way
        try:
            remote_port = self.request.connection.stream.socket.getpeername()[1]
        except Exception:  # pylint: disable=broad-except
            remote_port = 0

        return (remote_ip, remote_port)

    def getRemoteAddress(self):
        """
        Just for keeping same public interface
        """
        return self.srv_getRemoteAddress()

    def srv_getRemoteCredentials(self):
        """
        Get the credentials of the remote peer.

        :return: Credentials dictionary of remote peer.
        """
        return self.credDict

    def getRemoteCredentials(self):
        """
        Get the credentials of the remote peer.

        :return: Credentials dictionary of remote peer.
        """
        return self.credDict

    def srv_getFormattedRemoteCredentials(self):
        """
        Return the DN of user

        Mostly copy paste from
        :py:meth:`DIRAC.Core.DISET.private.Transports.BaseTransport.BaseTransport.getFormattedCredentials`

        Note that the information will be complete only once the AuthManager was called
        """
        address = self.getRemoteAddress()
        peerId = ""
        # Depending on where this is call, it may be that credDict is not yet filled.
        # (reminder: AuthQuery fills part of it..)
        try:
            peerId = "[%s:%s]" % (self.credDict.get("group", "visitor"), self.credDict.get("username", "anonymous"))
        except AttributeError:
            pass

        if address[0].find(":") > -1:
            return "([%s]:%s)%s" % (address[0], address[1], peerId)
        return "(%s:%s)%s" % (address[0], address[1], peerId)

    # Here we define all HTTP methods, but ONLY those defined in SUPPORTED_METHODS will be used.

    # Make a coroutine, see https://www.tornadoweb.org/en/branch5.1/guide/coroutines.html#coroutines for details
    @gen.coroutine
    def get(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``GET`` requests.
        .. note:: all the arguments are already prepared in the :py:meth:`.prepare` method.
        """
        # Execute the method in an executor (basically a separate thread)
        # Because of that, we cannot calls certain methods like `self.write`
        # in _executeMethod. This is because these methods are not threadsafe
        # https://www.tornadoweb.org/en/branch5.1/web.html#thread-safety-notes
        # However, we can still rely on instance attributes to store what should
        # be sent back (reminder: there is an instance of this class created for each request)
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)

    @gen.coroutine
    def post(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``POST`` requests."""
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)

    @gen.coroutine
    def head(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``HEAD`` requests."""
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)

    @gen.coroutine
    def delete(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``DELETE`` requests."""
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)

    @gen.coroutine
    def patch(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``PATCH`` requests."""
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)

    @gen.coroutine
    def put(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``PUT`` requests."""
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)

    @gen.coroutine
    def options(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """Method to handle incoming ``OPTIONS`` requests."""
        retVal = yield IOLoop.current().run_in_executor(*self.__prepareExecutor(args))
        self.__finishFuture(retVal)
