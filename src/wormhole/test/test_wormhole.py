from __future__ import print_function
import os, json, re, gc
from binascii import hexlify, unhexlify
import mock
from twisted.trial import unittest
from twisted.internet import reactor
from twisted.internet.defer import Deferred, gatherResults, inlineCallbacks
from .common import ServerBase
from .. import wormhole
from ..errors import WrongPasswordError, WelcomeError, UsageError
from spake2 import SPAKE2_Symmetric
from ..timing import DebugTiming
from nacl.secret import SecretBox

APPID = u"appid"

class MockWebSocket:
    def __init__(self):
        self._payloads = []
    def sendMessage(self, payload, is_binary):
        assert not is_binary
        self._payloads.append(payload)

    def outbound(self):
        out = []
        while self._payloads:
            p = self._payloads.pop(0)
            out.append(json.loads(p.decode("utf-8")))
        return out

def response(w, **kwargs):
    payload = json.dumps(kwargs).encode("utf-8")
    w._ws_dispatch_response(payload)

class Welcome(unittest.TestCase):
    def test_tolerate_no_current_version(self):
        w = wormhole._WelcomeHandler(u"relay_url", u"current_version", None)
        w.handle_welcome({})

    def test_print_motd(self):
        w = wormhole._WelcomeHandler(u"relay_url", u"current_version", None)
        with mock.patch("sys.stderr") as stderr:
            w.handle_welcome({u"motd": u"message of\nthe day"})
        self.assertEqual(stderr.method_calls,
                         [mock.call.write(u"Server (at relay_url) says:\n"
                                          " message of\n the day"),
                          mock.call.write(u"\n")])
        # motd is only displayed once
        with mock.patch("sys.stderr") as stderr2:
            w.handle_welcome({u"motd": u"second message"})
        self.assertEqual(stderr2.method_calls, [])

    def test_current_version(self):
        w = wormhole._WelcomeHandler(u"relay_url", u"2.0", None)
        with mock.patch("sys.stderr") as stderr:
            w.handle_welcome({u"current_version": u"2.0"})
        self.assertEqual(stderr.method_calls, [])

        with mock.patch("sys.stderr") as stderr:
            w.handle_welcome({u"current_version": u"3.0"})
        exp1 = (u"Warning: errors may occur unless both sides are"
                " running the same version")
        exp2 = (u"Server claims 3.0 is current, but ours is 2.0")
        self.assertEqual(stderr.method_calls,
                         [mock.call.write(exp1),
                          mock.call.write(u"\n"),
                          mock.call.write(exp2),
                          mock.call.write(u"\n"),
                          ])

        # warning is only displayed once
        with mock.patch("sys.stderr") as stderr:
            w.handle_welcome({u"current_version": u"3.0"})
        self.assertEqual(stderr.method_calls, [])

    def test_non_release_version(self):
        w = wormhole._WelcomeHandler(u"relay_url", u"2.0-dirty", None)
        with mock.patch("sys.stderr") as stderr:
            w.handle_welcome({u"current_version": u"3.0"})
        self.assertEqual(stderr.method_calls, [])

    def test_signal_error(self):
        se = mock.Mock()
        w = wormhole._WelcomeHandler(u"relay_url", u"2.0", se)
        w.handle_welcome({})
        self.assertEqual(se.mock_calls, [])

        w.handle_welcome({u"error": u"oops"})
        self.assertEqual(len(se.mock_calls), 1)
        self.assertEqual(len(se.mock_calls[0][1]), 1) # posargs
        we = se.mock_calls[0][1][0]
        self.assertIsInstance(we, WelcomeError)
        self.assertEqual(we.args, (u"oops",))
        # alas WelcomeError instances don't compare against each other
        #self.assertEqual(se.mock_calls, [mock.call(WelcomeError(u"oops"))])

class InputCode(unittest.TestCase):
    def test_list(self):
        send_command = mock.Mock()
        ic = wormhole._InputCode(None, u"prompt", 2, send_command,
                                 DebugTiming())
        d = ic._list()
        self.assertNoResult(d)
        self.assertEqual(send_command.mock_calls, [mock.call(u"list")])
        ic._response_handle_nameplates({u"type": u"nameplates",
                                        u"nameplates": [{u"id": u"123"}]})
        res = self.successResultOf(d)
        self.assertEqual(res, [u"123"])

class GetCode(unittest.TestCase):
    def test_get(self):
        send_command = mock.Mock()
        gc = wormhole._GetCode(2, send_command, DebugTiming())
        d = gc.go()
        self.assertNoResult(d)
        self.assertEqual(send_command.mock_calls, [mock.call(u"allocate")])
        # TODO: nameplate attributes get added and checked here
        gc._response_handle_allocated({u"type": u"allocated",
                                       u"nameplate": u"123"})
        code = self.successResultOf(d)
        self.assertIsInstance(code, type(u""))
        self.assert_(code.startswith(u"123-"))
        pieces = code.split(u"-")
        self.assertEqual(len(pieces), 3) # nameplate plus two words
        self.assert_(re.search(r'^\d+-\w+-\w+$', code), code)

class Basic(unittest.TestCase):
    def tearDown(self):
        # flush out any errorful Deferreds left dangling in cycles
        gc.collect()

    def check_out(self, out, **kwargs):
        # Assert that each kwarg is present in the 'out' dict. Ignore other
        # keys ('msgid' in particular)
        for key, value in kwargs.items():
            self.assertIn(key, out)
            self.assertEqual(out[key], value, (out, key, value))

    def check_outbound(self, ws, types):
        out = ws.outbound()
        self.assertEqual(len(out), len(types), (out, types))
        for i,t in enumerate(types):
            self.assertEqual(out[i][u"type"], t, (i,t,out))
        return out

    def make_pake(self, code, side, msg1):
        sp2 = SPAKE2_Symmetric(wormhole.to_bytes(code),
                               idSymmetric=wormhole.to_bytes(APPID))
        msg2 = sp2.start()
        msg2_hex = hexlify(msg2).decode("ascii")
        key = sp2.finish(msg1)
        return key, msg2_hex

    def test_create(self):
        wormhole._Wormhole(APPID, u"relay_url", reactor, None, None)

    def test_basic(self):
        # We don't call w._start(), so this doesn't create a WebSocket
        # connection. We provide a mock connection instead. If we wanted to
        # exercise _connect, we'd mock out WSFactory.
        # w._connect = lambda self: None
        # w._event_connected(mock_ws)
        # w._event_ws_opened()
        # w._ws_dispatch_response(payload)

        timing = DebugTiming()
        with mock.patch("wormhole.wormhole._WelcomeHandler") as wh_c:
            w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        wh = wh_c.return_value
        self.assertEqual(w._ws_url, u"relay_url")
        self.assertTrue(w._flag_need_nameplate)
        self.assertTrue(w._flag_need_to_build_msg1)
        self.assertTrue(w._flag_need_to_send_PAKE)

        v = w.verify()

        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        out = ws.outbound()
        self.assertEqual(len(out), 0)

        w._event_ws_opened(None)
        out = ws.outbound()
        self.assertEqual(len(out), 1)
        self.check_out(out[0], type=u"bind", appid=APPID, side=w._side)
        self.assertIn(u"id", out[0])

        # WelcomeHandler should get called upon 'welcome' response. Its full
        # behavior is exercised in 'Welcome' above.
        WELCOME = {u"foo": u"bar"}
        response(w, type="welcome", welcome=WELCOME)
        self.assertEqual(wh.mock_calls, [mock.call.handle_welcome(WELCOME)])

        # because we're connected, setting the code also claims the mailbox
        CODE = u"123-foo-bar"
        w.set_code(CODE)
        self.assertFalse(w._flag_need_to_build_msg1)
        out = ws.outbound()
        self.assertEqual(len(out), 1)
        self.check_out(out[0], type=u"claim", nameplate=u"123")

        # the server reveals the linked mailbox
        response(w, type=u"claimed", mailbox=u"mb456")

        # that triggers event_learned_mailbox, which should send open() and
        # PAKE
        self.assertEqual(w._mailbox_state, wormhole.OPEN)
        out = ws.outbound()
        self.assertEqual(len(out), 2)
        self.check_out(out[0], type=u"open", mailbox=u"mb456")
        self.check_out(out[1], type=u"add", phase=u"pake")
        self.assertNoResult(v)

        # server echoes back all "add" messages
        response(w, type=u"message", phase=u"pake", body=out[1][u"body"],
                 side=w._side)
        self.assertNoResult(v)

        # next we build the simulated peer's PAKE operation
        side2 = w._side + u"other"
        msg1 = unhexlify(out[1][u"body"].encode("ascii"))
        key, msg2_hex = self.make_pake(CODE, side2, msg1)
        response(w, type=u"message", phase=u"pake", body=msg2_hex, side=side2)

        # hearing the peer's PAKE (msg2) makes us release the nameplate, send
        # the confirmation message, and sends any queued phase messages. It
        # doesn't deliver the verifier because we're still waiting on the
        # confirmation message.
        self.assertFalse(w._flag_need_to_see_mailbox_used)
        self.assertEqual(w._key, key)
        out = ws.outbound()
        self.assertEqual(len(out), 2, out)
        self.check_out(out[0], type=u"release")
        self.check_out(out[1], type=u"add", phase=u"confirm")
        self.assertNoResult(v)

        # hearing a valid confirmation message doesn't throw an error
        confkey = w.derive_key(u"wormhole:confirmation", SecretBox.KEY_SIZE)
        nonce = os.urandom(wormhole.CONFMSG_NONCE_LENGTH)
        confirm2 = wormhole.make_confmsg(confkey, nonce)
        confirm2_hex = hexlify(confirm2).decode("ascii")
        response(w, type=u"message", phase=u"confirm", body=confirm2_hex,
                 side=side2)

        # and it releases the verifier
        verifier = self.successResultOf(v)
        self.assertEqual(verifier,
                         w.derive_key(u"wormhole:verifier", SecretBox.KEY_SIZE))

        # an outbound message can now be sent immediately
        w.send(b"phase0-outbound")
        out = ws.outbound()
        self.assertEqual(len(out), 1)
        self.check_out(out[0], type=u"add", phase=u"0")
        # decrypt+check the outbound message
        p0_outbound = unhexlify(out[0][u"body"].encode("ascii"))
        msgkey0 = w._derive_phase_key(w._side, u"0")
        p0_plaintext = w._decrypt_data(msgkey0, p0_outbound)
        self.assertEqual(p0_plaintext, b"phase0-outbound")

        # get() waits for the inbound message to arrive
        md = w.get()
        self.assertNoResult(md)
        self.assertIn(u"0", w._receive_waiters)
        self.assertNotIn(u"0", w._received_messages)
        msgkey1 = w._derive_phase_key(side2, u"0")
        p0_inbound = w._encrypt_data(msgkey1, b"phase0-inbound")
        p0_inbound_hex = hexlify(p0_inbound).decode("ascii")
        response(w, type=u"message", phase=u"0", body=p0_inbound_hex,
                 side=side2)
        p0_in = self.successResultOf(md)
        self.assertEqual(p0_in, b"phase0-inbound")
        self.assertNotIn(u"0", w._receive_waiters)
        self.assertIn(u"0", w._received_messages)

        # receiving an inbound message will queue it until get() is called
        msgkey2 = w._derive_phase_key(side2, u"1")
        p1_inbound = w._encrypt_data(msgkey2, b"phase1-inbound")
        p1_inbound_hex = hexlify(p1_inbound).decode("ascii")
        response(w, type=u"message", phase=u"1", body=p1_inbound_hex,
                 side=side2)
        self.assertIn(u"1", w._received_messages)
        self.assertNotIn(u"1", w._receive_waiters)
        p1_in = self.successResultOf(w.get())
        self.assertEqual(p1_in, b"phase1-inbound")
        self.assertIn(u"1", w._received_messages)
        self.assertNotIn(u"1", w._receive_waiters)

        d = w.close()
        self.assertNoResult(d)
        out = ws.outbound()
        self.assertEqual(len(out), 1)
        self.check_out(out[0], type=u"close", mood=u"happy")
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"released")
        self.assertEqual(w._drop_connection.mock_calls, [])
        response(w, type=u"closed")
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])
        w._ws_closed(True, None, None)
        self.assertEqual(self.successResultOf(d), None)

    def test_close_wait_0(self):
        # Close before the connection is established. The connection still
        # gets established, but it is then torn down before sending anything.
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()

        d = w.close()
        self.assertNoResult(d)

        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])
        self.assertNoResult(d)

        w._ws_closed(True, None, None)
        self.successResultOf(d)

    def test_close_wait_1(self):
        # close before even claiming the nameplate
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)

        d = w.close()
        self.check_outbound(ws, [u"bind"])
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])
        self.assertNoResult(d)

        w._ws_closed(True, None, None)
        self.successResultOf(d)

    def test_close_wait_2(self):
        # Close after claiming the nameplate, but before opening the mailbox.
        # The 'claimed' response arrives before we close.
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        CODE = u"123-foo-bar"
        w.set_code(CODE)
        self.check_outbound(ws, [u"bind", u"claim"])

        response(w, type=u"claimed", mailbox=u"mb123")

        d = w.close()
        self.check_outbound(ws, [u"open", u"add", u"release", u"close"])
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"released")
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"closed")
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])
        self.assertNoResult(d)

        w._ws_closed(True, None, None)
        self.successResultOf(d)

    def test_close_wait_3(self):
        # close after claiming the nameplate, but before opening the mailbox
        # The 'claimed' response arrives after we start to close.
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        CODE = u"123-foo-bar"
        w.set_code(CODE)
        self.check_outbound(ws, [u"bind", u"claim"])

        d = w.close()
        response(w, type=u"claimed", mailbox=u"mb123")
        self.check_outbound(ws, [u"release"])
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"released")
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])
        self.assertNoResult(d)

        w._ws_closed(True, None, None)
        self.successResultOf(d)

    def test_close_wait_4(self):
        # close after both claiming the nameplate and opening the mailbox
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        CODE = u"123-foo-bar"
        w.set_code(CODE)
        response(w, type=u"claimed", mailbox=u"mb456")
        self.check_outbound(ws, [u"bind", u"claim", u"open", u"add"])

        d = w.close()
        self.check_outbound(ws, [u"release", u"close"])
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"released")
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"closed")
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])

        w._ws_closed(True, None, None)
        self.successResultOf(d)

    def test_close_wait_5(self):
        # close after claiming the nameplate, opening the mailbox, then
        # releasing the nameplate
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        CODE = u"123-foo-bar"
        w.set_code(CODE)
        response(w, type=u"claimed", mailbox=u"mb456")

        w._key = b""
        msgkey = w._derive_phase_key(u"side2", u"misc")
        p1_inbound = w._encrypt_data(msgkey, b"")
        p1_inbound_hex = hexlify(p1_inbound).decode("ascii")
        response(w, type=u"message", phase=u"misc", side=u"side2",
                 body=p1_inbound_hex)
        self.check_outbound(ws, [u"bind", u"claim", u"open", u"add",
                                 u"release"])

        d = w.close()
        self.check_outbound(ws, [u"close"])
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"released")
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [])

        response(w, type=u"closed")
        self.assertNoResult(d)
        self.assertEqual(w._drop_connection.mock_calls, [mock.call()])

        w._ws_closed(True, None, None)
        self.successResultOf(d)

    def test_close_errbacks(self):
        # make sure the Deferreds returned by verify() and get() are properly
        # errbacked upon close
        pass

    def test_get_code_mock(self):
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        ws = MockWebSocket() # TODO: mock w._ws_send_command instead
        w._event_connected(ws)
        w._event_ws_opened(None)
        self.check_outbound(ws, [u"bind"])

        gc_c = mock.Mock()
        gc = gc_c.return_value = mock.Mock()
        gc_d = gc.go.return_value = Deferred()
        with mock.patch("wormhole.wormhole._GetCode", gc_c):
            d = w.get_code()
        self.assertNoResult(d)

        gc_d.callback(u"123-foo-bar")
        code = self.successResultOf(d)
        self.assertEqual(code, u"123-foo-bar")

    def test_get_code_real(self):
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        self.check_outbound(ws, [u"bind"])

        d = w.get_code()

        out = ws.outbound()
        self.assertEqual(len(out), 1)
        self.check_out(out[0], type=u"allocate")
        # TODO: nameplate attributes go here
        self.assertNoResult(d)

        response(w, type=u"allocated", nameplate=u"123")
        code = self.successResultOf(d)
        self.assertIsInstance(code, type(u""))
        self.assert_(code.startswith(u"123-"))
        pieces = code.split(u"-")
        self.assertEqual(len(pieces), 3) # nameplate plus two words
        self.assert_(re.search(r'^\d+-\w+-\w+$', code), code)

    # make sure verify() can be called both before and after the verifier is
    # computed

    def _test_verifier(self, when, order, success):
        assert when in ("early", "middle", "late")
        assert order in ("key-then-confirm", "confirm-then-key")
        assert isinstance(success, bool)
        #print(when, order, success)

        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        w._ws_send_command = mock.Mock()
        w._mailbox_state = wormhole.OPEN
        d = None

        if success:
            w._key = b"key"
        else:
            w._key = b"wrongkey"
        confkey = w._derive_confirmation_key()
        nonce = os.urandom(wormhole.CONFMSG_NONCE_LENGTH)
        confmsg = wormhole.make_confmsg(confkey, nonce)
        w._key = None

        if when == "early":
            d = w.verify()
            self.assertNoResult(d)

        if order == "key-then-confirm":
            w._key = b"key"
            w._event_established_key()
        else:
            w._event_received_confirm(confmsg)

        if when == "middle":
            d = w.verify()
        if d:
            self.assertNoResult(d) # still waiting for other msg

        if order == "confirm-then-key":
            w._key = b"key"
            w._event_established_key()
        else:
            w._event_received_confirm(confmsg)

        if when == "late":
            d = w.verify()
        if success:
            self.successResultOf(d)
        else:
            self.assertFailure(d, wormhole.WrongPasswordError)
            self.flushLoggedErrors(WrongPasswordError)

    def test_verifier(self):
        for when in ("early", "middle", "late"):
            for order in ("key-then-confirm", "confirm-then-key"):
                for success in (False, True):
                    self._test_verifier(when, order, success)


    def test_api_errors(self):
        # doing things you're not supposed to do
        pass

    def test_welcome_error(self):
        # A welcome message could arrive at any time, with an [error] key
        # that should make us halt. In practice, though, this gets sent as
        # soon as the connection is established, which limits the possible
        # states in which we might see it.

        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        self.check_outbound(ws, [u"bind"])

        d1 = w.get()
        d2 = w.verify()
        d3 = w.get_code()
        # TODO (tricky): test w.input_code

        self.assertNoResult(d1)
        self.assertNoResult(d2)
        self.assertNoResult(d3)

        w._signal_error(WelcomeError(u"you are not actually welcome"), u"pouty")
        self.failureResultOf(d1, WelcomeError)
        self.failureResultOf(d2, WelcomeError)
        self.failureResultOf(d3, WelcomeError)

        # once the error is signalled, all API calls should fail
        self.assertRaises(WelcomeError, w.send, u"foo")
        self.assertRaises(WelcomeError,
                          w.derive_key, u"foo", SecretBox.KEY_SIZE)
        self.failureResultOf(w.get(), WelcomeError)
        self.failureResultOf(w.verify(), WelcomeError)

    def test_confirm_error(self):
        # we should only receive the "confirm" message after we receive the
        # PAKE message, by which point we should know the key. If the
        # confirmation message doesn't decrypt, we signal an error.
        timing = DebugTiming()
        w = wormhole._Wormhole(APPID, u"relay_url", reactor, None, timing)
        w._drop_connection = mock.Mock()
        ws = MockWebSocket()
        w._event_connected(ws)
        w._event_ws_opened(None)
        w.set_code(u"123-foo-bar")
        response(w, type=u"claimed", mailbox=u"mb456")

        d1 = w.get()
        d2 = w.verify()
        self.assertNoResult(d1)
        self.assertNoResult(d2)

        out = ws.outbound()
        # [u"bind", u"claim", u"open", u"add"]
        self.assertEqual(len(out), 4)
        self.assertEqual(out[3][u"type"], u"add")

        sp2 = SPAKE2_Symmetric(b"", idSymmetric=wormhole.to_bytes(APPID))
        msg2 = sp2.start()
        msg2_hex = hexlify(msg2).decode("ascii")
        response(w, type=u"message", phase=u"pake", body=msg2_hex, side=u"s2")
        self.assertNoResult(d1)
        self.assertNoResult(d2) # verify() waits for confirmation

        # sending a random confirm message will cause a confirmation error
        confkey = w.derive_key(u"WRONG", SecretBox.KEY_SIZE)
        nonce = os.urandom(wormhole.CONFMSG_NONCE_LENGTH)
        badconfirm = wormhole.make_confmsg(confkey, nonce)
        badconfirm_hex = hexlify(badconfirm).decode("ascii")
        response(w, type=u"message", phase=u"confirm", body=badconfirm_hex,
                 side=u"s2")

        self.failureResultOf(d1, WrongPasswordError)
        self.failureResultOf(d2, WrongPasswordError)

        # once the error is signalled, all API calls should fail
        self.assertRaises(WrongPasswordError, w.send, u"foo")
        self.assertRaises(WrongPasswordError,
                          w.derive_key, u"foo", SecretBox.KEY_SIZE)
        self.failureResultOf(w.get(), WrongPasswordError)
        self.failureResultOf(w.verify(), WrongPasswordError)


# event orderings to exercise:
#
# * normal sender: set_code, send_phase1, connected, claimed, learn_msg2,
#   learn_phase1
# * normal receiver (argv[2]=code): set_code, connected, learn_msg1,
#   learn_phase1, send_phase1,
# * normal receiver (readline): connected, input_code
# *
# * set_code, then connected
# * connected, receive_pake, send_phase, set_code

class Wormholes(ServerBase, unittest.TestCase):
    # integration test, with a real server

    def doBoth(self, d1, d2):
        return gatherResults([d1, d2], True)

    @inlineCallbacks
    def test_basic(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        code = yield w1.get_code()
        w2.set_code(code)
        w1.send(b"data1")
        w2.send(b"data2")
        dataX = yield w1.get()
        dataY = yield w2.get()
        self.assertEqual(dataX, b"data2")
        self.assertEqual(dataY, b"data1")
        yield w1.close()
        yield w2.close()

    @inlineCallbacks
    def test_same_message(self):
        # the two sides use random nonces for their messages, so it's ok for
        # both to try and send the same body: they'll result in distinct
        # encrypted messages
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        code = yield w1.get_code()
        w2.set_code(code)
        w1.send(b"data")
        w2.send(b"data")
        dataX = yield w1.get()
        dataY = yield w2.get()
        self.assertEqual(dataX, b"data")
        self.assertEqual(dataY, b"data")
        yield w1.close()
        yield w2.close()

    @inlineCallbacks
    def test_interleaved(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        code = yield w1.get_code()
        w2.set_code(code)
        w1.send(b"data1")
        dataY = yield w2.get()
        self.assertEqual(dataY, b"data1")
        d = w1.get()
        w2.send(b"data2")
        dataX = yield d
        self.assertEqual(dataX, b"data2")
        yield w1.close()
        yield w2.close()

    @inlineCallbacks
    def test_unidirectional(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        code = yield w1.get_code()
        w2.set_code(code)
        w1.send(b"data1")
        dataY = yield w2.get()
        self.assertEqual(dataY, b"data1")
        yield w1.close()
        yield w2.close()

    @inlineCallbacks
    def test_early(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w1.send(b"data1")
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        d = w2.get()
        w1.set_code(u"123-abc-def")
        w2.set_code(u"123-abc-def")
        dataY = yield d
        self.assertEqual(dataY, b"data1")
        yield w1.close()
        yield w2.close()

    @inlineCallbacks
    def test_fixed_code(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w1.set_code(u"123-purple-elephant")
        w2.set_code(u"123-purple-elephant")
        w1.send(b"data1"), w2.send(b"data2")
        dl = yield self.doBoth(w1.get(), w2.get())
        (dataX, dataY) = dl
        self.assertEqual(dataX, b"data2")
        self.assertEqual(dataY, b"data1")
        yield w1.close()
        yield w2.close()


    @inlineCallbacks
    def test_multiple_messages(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w1.set_code(u"123-purple-elephant")
        w2.set_code(u"123-purple-elephant")
        w1.send(b"data1"), w2.send(b"data2")
        w1.send(b"data3"), w2.send(b"data4")
        dl = yield self.doBoth(w1.get(), w2.get())
        (dataX, dataY) = dl
        self.assertEqual(dataX, b"data2")
        self.assertEqual(dataY, b"data1")
        dl = yield self.doBoth(w1.get(), w2.get())
        (dataX, dataY) = dl
        self.assertEqual(dataX, b"data4")
        self.assertEqual(dataY, b"data3")
        yield w1.close()
        yield w2.close()

    @inlineCallbacks
    def test_wrong_password(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        code = yield w1.get_code()
        w2.set_code(code+"not")
        # That's enough to allow both sides to discover the mismatch, but
        # only after the confirmation message gets through. API calls that
        # don't wait will appear to work until the mismatched confirmation
        # message arrives.
        w1.send(b"should still work")
        w2.send(b"should still work")

        # API calls that wait (i.e. get) will errback
        yield self.assertFailure(w2.get(), WrongPasswordError)
        yield self.assertFailure(w1.get(), WrongPasswordError)

        yield w1.close()
        yield w2.close()
        self.flushLoggedErrors(WrongPasswordError)

    @inlineCallbacks
    def test_verifier(self):
        w1 = wormhole.wormhole(APPID, self.relayurl, reactor)
        w2 = wormhole.wormhole(APPID, self.relayurl, reactor)
        code = yield w1.get_code()
        w2.set_code(code)
        v1 = yield w1.verify()
        v2 = yield w2.verify()
        self.failUnlessEqual(type(v1), type(b""))
        self.failUnlessEqual(v1, v2)
        w1.send(b"data1")
        w2.send(b"data2")
        dataX = yield w1.get()
        dataY = yield w2.get()
        self.assertEqual(dataX, b"data2")
        self.assertEqual(dataY, b"data1")
        yield w1.close()
        yield w2.close()

class Errors(ServerBase, unittest.TestCase):
    @inlineCallbacks
    def test_codes_1(self):
        w = wormhole.wormhole(APPID, self.relayurl, reactor)
        # definitely too early
        self.assertRaises(UsageError, w.derive_key, u"purpose", 12)

        w.set_code(u"123-purple-elephant")
        # code can only be set once
        self.assertRaises(UsageError, w.set_code, u"123-nope")
        yield self.assertFailure(w.get_code(), UsageError)
        yield self.assertFailure(w.input_code(), UsageError)
        yield w.close()

    @inlineCallbacks
    def test_codes_2(self):
        w = wormhole.wormhole(APPID, self.relayurl, reactor)
        yield w.get_code()
        self.assertRaises(UsageError, w.set_code, u"123-nope")
        yield self.assertFailure(w.get_code(), UsageError)
        yield self.assertFailure(w.input_code(), UsageError)
        yield w.close()
