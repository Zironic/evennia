"""
Typeclass for Player objects

Note that this object is primarily intended to
store OOC information, not game info! This
object represents the actual user (not their
character) and has NO actual precence in the
game world (this is handled by the associated
character object, so you should customize that
instead for most things).

"""

import datetime
from django.conf import settings
from evennia.typeclasses.models import TypeclassBase
from evennia.players.manager import PlayerManager
from evennia.players.models import PlayerDB
from evennia.comms.models import ChannelDB
from evennia.commands import cmdhandler
from evennia.scripts.models import ScriptDB
from evennia.utils import logger
from evennia.utils.utils import (lazy_property, to_str,
                             make_iter, to_unicode,
                             variable_from_module)
from evennia.typeclasses.attributes import NickHandler
from evennia.scripts.scripthandler import ScriptHandler
from evennia.commands.cmdsethandler import CmdSetHandler

from django.utils.translation import ugettext as _

__all__ = ("DefaultPlayer",)

_SESSIONS = None

_AT_SEARCH_RESULT = variable_from_module(*settings.SEARCH_AT_RESULT.rsplit('.', 1))
_MULTISESSION_MODE = settings.MULTISESSION_MODE
_CMDSET_PLAYER = settings.CMDSET_PLAYER
_CONNECT_CHANNEL = None

class DefaultPlayer(PlayerDB):
    """
    This is the base Typeclass for all Players. Players represent
    the person playing the game and tracks account info, password
    etc. They are OOC entities without presence in-game. A Player
    can connect to a Character Object in order to "enter" the
    game.

    Player Typeclass API:

    * Available properties (only available on initiated typeclass objects)

     key (string) - name of player
     name (string)- wrapper for user.username
     aliases (list of strings) - aliases to the object. Will be saved to
                        database as AliasDB entries but returned as strings.
     dbref (int, read-only) - unique #id-number. Also "id" can be used.
     date_created (string) - time stamp of object creation
     permissions (list of strings) - list of permission strings

     user (User, read-only) - django User authorization object
     obj (Object) - game object controlled by player. 'character' can also
                    be used.
     sessions (list of Sessions) - sessions connected to this player
     is_superuser (bool, read-only) - if the connected user is a superuser

    * Handlers

     locks - lock-handler: use locks.add() to add new lock strings
     db - attribute-handler: store/retrieve database attributes on this
                             self.db.myattr=val, val=self.db.myattr
     ndb - non-persistent attribute handler: same as db but does not
                                 create a database entry when storing data
     scripts - script-handler. Add new scripts to object with scripts.add()
     cmdset - cmdset-handler. Use cmdset.add() to add new cmdsets to object
     nicks - nick-handler. New nicks with nicks.add().

    * Helper methods

     msg(outgoing_string, from_obj=None, **kwargs)
     #swap_character(new_character, delete_old_character=False)
     execute_cmd(raw_string)
     search(ostring, global_search=False, attribute_name=None,
                     use_nicks=False, location=None,
                     ignore_errors=False, player=False)
     is_typeclass(typeclass, exact=False)
     swap_typeclass(new_typeclass, clean_attributes=False, no_default=True)
     access(accessing_obj, access_type='read', default=False)
     check_permstring(permstring)

    * Hook methods

     basetype_setup()
     at_player_creation()

     - note that the following hooks are also found on Objects and are
       usually handled on the character level:

     at_init()
     at_access()
     at_cmdset_get(**kwargs)
     at_first_login()
     at_post_login(sessid=None)
     at_disconnect()
     at_message_receive()
     at_message_send()
     at_server_reload()
     at_server_shutdown()

     """

    __metaclass__ = TypeclassBase
    objects = PlayerManager()

    # properties
    @lazy_property
    def cmdset(self):
        return CmdSetHandler(self, True)

    @lazy_property
    def scripts(self):
        return ScriptHandler(self)

    @lazy_property
    def nicks(self):
        return NickHandler(self)


    # session-related methods

    def get_session(self, sessid):
        """
        Return session with given sessid connected to this player.
        note that the sessionhandler also accepts sessid as an iterable.
        """
        global _SESSIONS
        if not _SESSIONS:
            from evennia.server.sessionhandler import SESSIONS as _SESSIONS
        return _SESSIONS.session_from_player(self, sessid)

    def get_all_sessions(self):
        "Return all sessions connected to this player"
        global _SESSIONS
        if not _SESSIONS:
            from evennia.server.sessionhandler import SESSIONS as _SESSIONS
        return _SESSIONS.sessions_from_player(self)
    sessions = property(get_all_sessions)  # alias shortcut

    def disconnect_session_from_player(self, sessid):
        """
        Access method for disconnecting a given session from the player
        (connection happens automatically in the sessionhandler)
        """
        # this should only be one value, loop just to make sure to
        # clean everything
        sessions = (session for session in self.get_all_sessions()
                    if session.sessid == sessid)
        for session in sessions:
            # this will also trigger unpuppeting
            session.sessionhandler.disconnect(session)

    # puppeting operations

    def puppet_object(self, sessid, obj, normal_mode=True):
        """
        Use the given session to control (puppet) the given object (usually
        a Character type).

        sessid - session id of session to connect
        obj - the object to connect to
        normal_mode - trigger hooks and extra checks - this is turned off when
                     the server reloads, to quickly re-connect puppets.

        returns True if successful, False otherwise
        """
        # safety checks
        if not obj:
            return
        session = self.get_session(sessid)
        if not session:
            return False
        if self.get_puppet(sessid) == obj:
            # already puppeting this object
            return
        if not obj.access(self, 'puppet'):
            # no access
            self.msg("You don't have permission to puppet '%s'." % obj.key)
            return
        if normal_mode and obj.player:
            # object already puppeted
            if obj.player == self:
                if obj.sessid.count():
                    # we may take over another of our sessions
                    # output messages to the affected sessions
                    if _MULTISESSION_MODE in (1, 3):
                        txt1 = "{c%s{n{G is now shared from another of your sessions.{n"
                        txt2 = "Sharing {c%s{n with another of your sessions."
                    else:
                        txt1 = "{c%s{n{R is now acted from another of your sessions.{n"
                        txt2 = "Taking over {c%s{n from another of your sessions."
                    self.msg(txt1 % obj.name, sessid=obj.sessid.get())
                    self.msg(txt2 % obj.name, sessid=sessid)
                    self.unpuppet_object(obj.sessid.get())
            elif obj.player.is_connected:
                # controlled by another player
                self.msg("{R{c%s{R is already puppeted by another Player.")
                return

        # do the puppeting
        if normal_mode and session.puppet:
            # cleanly unpuppet eventual previous object puppeted by this session
            self.unpuppet_object(sessid)
        # if we get to this point the character is ready to puppet or it
        # was left with a lingering player/sessid reference from an unclean
        # server kill or similar

        if normal_mode:
            obj.at_pre_puppet(self, sessid=sessid)
        # do the connection
        obj.sessid.add(sessid)
        obj.player = self
        session.puid = obj.id
        session.puppet = obj
        # validate/start persistent scripts on object
        ScriptDB.objects.validate(obj=obj)
        if normal_mode:
            obj.at_post_puppet()
        # re-cache locks to make sure superuser bypass is updated
        obj.locks.cache_lock_bypass(obj)
        return True

    def unpuppet_object(self, sessid):
        """
        Disengage control over an object

        sessid - the session id to disengage

        returns True if successful
        """
        session = self.get_session(sessid)
        if not session:
            return False
        session = make_iter(session)[0]
        #print "unpuppet, session:", session, session.puppet
        obj = hasattr(session, "puppet") and session.puppet or None
        #print "unpuppet, obj:", obj
        if not obj:
            return False
        # do the disconnect, but only if we are the last session to puppet
        obj.at_pre_unpuppet()
        obj.sessid.remove(sessid)
        if not obj.sessid.count():
            del obj.player
            obj.at_post_unpuppet(self, sessid=sessid)
        session.puppet = None
        session.puid = None
        return True

    def unpuppet_all(self):
        """
        Disconnect all puppets. This is called by server
        before a reset/shutdown.
        """
        for session in self.get_all_sessions():
            self.unpuppet_object(session.sessid)

    def get_puppet(self, sessid, return_dbobj=False):
        """
        Get an object puppeted by this session through this player. This is
        the main method for retrieving the puppeted object from the
        player's end.

        sessid - return character connected to this sessid,

        """
        session = self.get_session(sessid)
        if not session:
            return None
        if return_dbobj:
            return session.puppet
        return session.puppet and session.puppet or None

    def get_all_puppets(self, return_dbobj=False):
        """
        Get all currently puppeted objects as a list
        """
        puppets = [session.puppet for session in self.get_all_sessions()
                                                            if session.puppet]
        if return_dbobj:
            return puppets
        return [puppet for puppet in puppets]

    def __get_single_puppet(self):
        """
        This is a legacy convenience link for users of
        MULTISESSION_MODE 0 or 1. It will return
        only the first puppet. For mode 2, this returns
        a list of all characters.
        """
        puppets = self.get_all_puppets()
        if _MULTISESSION_MODE in (0, 1):
            return puppets and puppets[0] or None
        return puppets
    character = property(__get_single_puppet)
    puppet = property(__get_single_puppet)

    # utility methods

    def delete(self, *args, **kwargs):
        """
        Deletes the player permanently.
        """
        for session in self.get_all_sessions():
            # unpuppeting all objects and disconnecting the user, if any
            # sessions remain (should usually be handled from the
            # deleting command)
            self.unpuppet_object(session.sessid)
            session.sessionhandler.disconnect(session, reason=_("Player being deleted."))
        self.scripts.stop()
        self.attributes.clear()
        self.nicks.clear()
        self.aliases.clear()
        super(PlayerDB, self).delete(*args, **kwargs)
    ## methods inherited from database model

    def msg(self, text=None, from_obj=None, sessid=None, **kwargs):
        """
        Evennia -> User
        This is the main route for sending data back to the user from the
        server.

        outgoing_string (string) - text data to send
        from_obj (Object/Player) - source object of message to send. Its
                 at_msg_send() hook will be called.
        sessid - the session id of the session to send to. If not given, return
                 to all sessions connected to this player. This is usually only
                 relevant when using msg() directly from a player-command (from
                 a command on a Character, the character automatically stores
                 and handles the sessid). Can also be a list of sessids.
        kwargs (dict) - All other keywords are parsed as extra data.
        """
        if "data" in kwargs:
            # deprecation warning
            logger.log_depmsg("PlayerDB:msg() 'data'-dict keyword is deprecated. Use **kwargs instead.")
            data = kwargs.pop("data")
            if isinstance(data, dict):
                kwargs.update(data)

        text = to_str(text, force_string=True) if text else ""
        if from_obj:
            # call hook
            try:
                from_obj.at_msg_send(text=text, to_obj=self, **kwargs)
            except Exception:
                pass
        sessions = _MULTISESSION_MODE > 1 and sessid and self.get_session(sessid) or None
        if sessions:
            for session in make_iter(sessions):
                obj = session.puppet
                if obj and not obj.at_msg_receive(text=text, **kwargs):
                    # if hook returns false, cancel send
                    continue
                session.msg(text=text, **kwargs)
        else:
            # if no session was specified, send to them all
            for sess in self.get_all_sessions():
                sess.msg(text=text, **kwargs)

    def execute_cmd(self, raw_string, sessid=None, **kwargs):
        """
        Do something as this player. This method is never called normally,
        but only when the player object itself is supposed to execute the
        command. It takes player nicks into account, but not nicks of
        eventual puppets.

        raw_string - raw command input coming from the command line.
        sessid - the optional session id to be responsible for the command-send
        **kwargs - other keyword arguments will be added to the found command
                   object instace as variables before it executes. This is
                   unused by default Evennia but may be used to set flags and
                   change operating paramaters for commands at run-time.
        """
        raw_string = to_unicode(raw_string)
        raw_string = self.nicks.nickreplace(raw_string,
                          categories=("inputline", "channel"), include_player=False)
        if not sessid and _MULTISESSION_MODE in (0, 1):
            # in this case, we should either have only one sessid, or the sessid
            # should not matter (since the return goes to all of them we can
            # just use the first one as the source)
            try:
                sessid = self.get_all_sessions()[0].sessid
            except IndexError:
                # this can happen for bots
                sessid = None
        return cmdhandler.cmdhandler(self, raw_string,
                                     callertype="player", sessid=sessid, **kwargs)

    def search(self, searchdata, return_puppet=False,
               nofound_string=None, multimatch_string=None, **kwargs):
        """
        This is similar to the ObjectDB search method but will search for
        Players only. Errors will be echoed, and None returned if no Player
        is found.
        searchdata - search criterion, the Player's key or dbref to search for
        return_puppet  - will try to return the object the player controls
                           instead of the Player object itself. If no
                           puppeted object exists (since Player is OOC), None will
                           be returned.
        nofound_string - optional custom string for not-found error message.
        multimatch_string - optional custom string for multimatch error header.
        Extra keywords are ignored, but are allowed in call in order to make
                           API more consistent with objects.models.TypedObject.search.
        """
        # handle me, self and *me, *self
        if isinstance(searchdata, basestring):
            # handle wrapping of common terms
            if searchdata.lower() in ("me", "*me", "self", "*self",):
                return self
        matches = self.__class__.objects.player_search(searchdata)
        matches = _AT_SEARCH_RESULT(self, searchdata, matches, global_search=True,
                                    nofound_string=nofound_string,
                                    multimatch_string=multimatch_string)
        if matches and return_puppet:
            try:
                return matches.puppet
            except AttributeError:
                return None
        return matches

    def is_typeclass(self, typeclass, exact=False):
        """
        Returns true if this object has this type
          OR has a typeclass which is an subclass of
          the given typeclass.

        typeclass - can be a class object or the
                python path to such an object to match against.

        exact - returns true only if the object's
               type is exactly this typeclass, ignoring
               parents.

        Returns: Boolean
        """
        return super(DefaultPlayer, self).is_typeclass(typeclass, exact=exact)

    def swap_typeclass(self, new_typeclass, clean_attributes=False, no_default=True):
        """
        This performs an in-situ swap of the typeclass. This means
        that in-game, this object will suddenly be something else.
        Player will not be affected. To 'move' a player to a different
        object entirely (while retaining this object's type), use
        self.player.swap_object().

        Note that this might be an error prone operation if the
        old/new typeclass was heavily customized - your code
        might expect one and not the other, so be careful to
        bug test your code if using this feature! Often its easiest
        to create a new object and just swap the player over to
        that one instead.

        Arguments:
        new_typeclass (path/classobj) - type to switch to
        clean_attributes (bool/list) - will delete all attributes
                           stored on this object (but not any
                           of the database fields such as name or
                           location). You can't get attributes back,
                           but this is often the safest bet to make
                           sure nothing in the new typeclass clashes
                           with the old one. If you supply a list,
                           only those named attributes will be cleared.
        no_default - if this is active, the swapper will not allow for
                     swapping to a default typeclass in case the given
                     one fails for some reason. Instead the old one
                     will be preserved.
        Returns:
          boolean True/False depending on if the swap worked or not.

        """
        super(DefaultPlayer, self).swap_typeclass(new_typeclass,
                    clean_attributes=clean_attributes, no_default=no_default)

    def access(self, accessing_obj, access_type='read', default=False, **kwargs):
        """
        Determines if another object has permission to access this object
        in whatever way.

          accessing_obj (Object)- object trying to access this one
          access_type (string) - type of access sought
          default (bool) - what to return if no lock of access_type was found
          **kwargs - passed to the at_access hook along with the result.
        """
        result = super(DefaultPlayer, self).access(accessing_obj, access_type=access_type, default=default)
        self.at_access(result, accessing_obj, access_type, **kwargs)
        return result

    def check_permstring(self, permstring):
        """
        This explicitly checks the given string against this object's
        'permissions' property without involving any locks.

        permstring (string) - permission string that need to match a permission
                              on the object. (example: 'Builders')
        Note that this method does -not- call the at_access hook.
        """
        return super(DefaultPlayer, self).check_permstring(permstring)

    ## player hooks

    def basetype_setup(self):
        """
        This sets up the basic properties for a player.
        Overload this with at_player_creation rather than
        changing this method.

        """
        # A basic security setup
        lockstring = "examine:perm(Wizards);edit:perm(Wizards);delete:perm(Wizards);boot:perm(Wizards);msg:all()"
        self.locks.add(lockstring)

        # The ooc player cmdset
        self.cmdset.add_default(_CMDSET_PLAYER, permanent=True)

    def at_player_creation(self):
        """
        This is called once, the very first time
        the player is created (i.e. first time they
        register with the game). It's a good place
        to store attributes all players should have,
        like configuration values etc.
        """
        # set an (empty) attribute holding the characters this player has
        lockstring = "attrread:perm(Admins);attredit:perm(Admins);attrcreate:perm(Admins)"
        self.attributes.add("_playable_characters", [], lockstring=lockstring)

    def at_init(self):
        """
        This is always called whenever this object is initiated --
        that is, whenever it its typeclass is cached from memory. This
        happens on-demand first time the object is used or activated
        in some way after being created but also after each server
        restart or reload. In the case of player objects, this usually
        happens the moment the player logs in or reconnects after a
        reload.
        """
        pass


    # Note that the hooks below also exist in the character object's
    # typeclass. You can often ignore these and rely on the character
    # ones instead, unless you are implementing a multi-character game
    # and have some things that should be done regardless of which
    # character is currently connected to this player.

    def at_first_save(self):
        """
        This is a generic hook called by Evennia when this object is
        saved to the database the very first time.  You generally
        don't override this method but the hooks called by it.
        """
        self.basetype_setup()
        self.at_player_creation()

        permissions = settings.PERMISSION_PLAYER_DEFAULT
        if hasattr(self, "_createdict"):
            # this will only be set if the utils.create_player
            # function was used to create the object.
            cdict = self._createdict
            if cdict.get("locks"):
                self.locks.add(cdict["locks"])
            if cdict.get("permissions"):
                permissions = cdict["permissions"]
            del self._createdict

        self.permissions.add(permissions)

    def at_access(self, result, accessing_obj, access_type, **kwargs):
        """
        This is called with the result of an access call, along with
        any kwargs used for that call. The return of this method does
        not affect the result of the lock check. It can be used e.g. to
        customize error messages in a central location or other effects
        based on the access result.
        """
        pass

    def at_cmdset_get(self, **kwargs):
        """
        Called just before cmdsets on this player are requested by the
        command handler. If changes need to be done on the fly to the
        cmdset before passing them on to the cmdhandler, this is the
        place to do it.  This is called also if the player currently
        have no cmdsets.  kwargs are usually not used unless the
        cmdset is generated dynamically.
        """
        pass

    def at_first_login(self):
        """
        Called the very first time this player logs into the game.
        """
        pass

    def at_pre_login(self):
        """
        Called every time the user logs in, just before the actual
        login-state is set.
        """
        pass

    def _send_to_connect_channel(self, message):
        "Helper method for loading the default comm channel"
        global _CONNECT_CHANNEL
        if not _CONNECT_CHANNEL:
            try:
                _CONNECT_CHANNEL = ChannelDB.objects.filter(db_key=settings.DEFAULT_CHANNELS[1]["key"])[0]
            except Exception:
                logger.log_trace()
        now = datetime.datetime.now()
        now = "%02i-%02i-%02i(%02i:%02i)" % (now.year, now.month,
                                             now.day, now.hour, now.minute)
        if _CONNECT_CHANNEL:
            _CONNECT_CHANNEL.tempmsg("[%s, %s]: %s" % (_CONNECT_CHANNEL.key, now, message))
        else:
            logger.log_infomsg("[%s]: %s" % (now, message))

    def at_post_login(self, sessid=None):
        """
        Called at the end of the login process, just before letting
        the player loose. This is called before an eventual Character's
        at_post_login hook.
        """
        self._send_to_connect_channel("{G%s connected{n" % self.key)
        if _MULTISESSION_MODE == 0:
            # in this mode we should have only one character available. We
            # try to auto-connect to our last conneted object, if any
            self.puppet_object(sessid, self.db._last_puppet)
        elif _MULTISESSION_MODE == 1:
            # in this mode the first session to connect acts like mode 0,
            # the following sessions "share" the same view and should
            # not perform any actions
            if not self.get_all_puppets():
                # we are first. Connect.
                self.puppet_object(sessid, self.db._last_puppet)
        elif _MULTISESSION_MODE in (2, 3):
            # In this mode we by default end up at a character selection
            # screen. We execute look on the player.
            self.execute_cmd("look", sessid=sessid)

    def at_disconnect(self, reason=None):
        """
        Called just before user is disconnected.
        """
        reason = reason and "(%s)" % reason or ""
        self._send_to_connect_channel("{R%s disconnected %s{n" % (self.key, reason))

    def at_post_disconnect(self):
        """
        This is called after disconnection is complete. No messages
        can be relayed to the player from here. After this call, the
        player should not be accessed any more, making this a good
        spot for deleting it (in the case of a guest player account,
        for example).
        """
        pass

    def at_message_receive(self, message, from_obj=None):
        """
        Called when any text is emitted to this
        object. If it returns False, no text
        will be sent automatically.
        """
        return True

    def at_message_send(self, message, to_object):
        """
        Called whenever this object tries to send text
        to another object. Only called if the object supplied
        itself as a sender in the msg() call.
        """
        pass

    def at_server_reload(self):
        """
        This hook is called whenever the server is shutting down for
        restart/reboot. If you want to, for example, save non-persistent
        properties across a restart, this is the place to do it.
        """
        pass

    def at_server_shutdown(self):
        """
        This hook is called whenever the server is shutting down fully
        (i.e. not for a restart).
        """
        pass


class DefaultGuest(DefaultPlayer):
    """
    This class is used for guest logins. Unlike Players, Guests and their
    characters are deleted after disconnection.
    """
    def at_post_login(self, sessid=None):
        """
        In theory, guests only have one character regardless of which
        MULTISESSION_MODE we're in. They don't get a choice.
        """
        self._send_to_connect_channel("{G%s connected{n" % self.key)
        self._go_ic_at_login(sessid=sessid)

    def at_disconnect(self):
        """
        A Guest's characters aren't meant to linger on the server. When a
        Guest disconnects, we remove its character.
        """
        super(DefaultGuest, self).at_disconnect()
        characters = self.db._playable_characters
        for character in filter(None, characters):
            character.delete()

    def at_server_shutdown(self):
        """
        We repeat at_disconnect() here just to be on the safe side.
        """
        super(DefaultGuest, self).at_server_shutdown()
        characters = self.db._playable_characters
        for character in filter(None, characters):
            character.delete()

    def at_post_disconnect(self):
        """
        Guests aren't meant to linger on the server, either. We need to wait
        until after the Guest disconnects to delete it, though.
        """
        super(DefaultGuest, self).at_post_disconnect()
        self.delete()
