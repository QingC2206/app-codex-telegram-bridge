"""Approved interaction fixes: all model, Desktop and Telegram calls are mocked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_telegram_bridge.core import bot as b
from codex_telegram_bridge.features.elicitation import form_content, form_questions
from codex_telegram_bridge.telegram import notifications as n
import test_controller_delivery as delivery_fixture


class InteractionFixTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.bot = b.TelegramCodexBot('111:fixture', {'chat_id': '123'}, app_factory=lambda *a: Mock())
        self.bot.state = b.StateStore(self.root/'state.json')
        self.bot.send = Mock(return_value={'message_id': 101})
        self.bot.edit_message = Mock()
        self.bot.enqueue = Mock()
        self.bot._reaction = Mock(return_value=False)
        self.bot._submission_feedback = Mock()
        self.bot._input_error = Mock()
        self.bot.desktop = Mock()
        self.bot.sides.lookup = Mock(return_value=None)
        self.bot.async_inputs.reply_to = Mock(return_value=False)
        p = patch.object(n, 'NATIVE_REPLY_DIR', self.root/'routes'); p.start(); self.addCleanup(p.stop)

    def choice(self, current='new'):
        self.bot.state.update(managed_threads={'task': {'origin':'bot_created'}})
        if current: self.bot.active_turns['task'] = current
        self.bot.pending_direct_replies['seq'] = {'thread_id':'task', 'turn_id':'old', 'text':'intent'}

    def question(self):
        self.bot.on_server_request({'id':1, 'method':'item/tool/requestUserInput', 'params':{
            'threadId':'task', 'questions':[
                {'id':'q1','options':[{'label':'A1'}]},
                {'id':'q2','options':[{'label':'A2'}]}]}})
        return next(iter(self.bot.pending_questions))

    def form(self, required=None):
        params = {'threadId':'task','mode':'form','requestedSchema':{'type':'object',
            'properties':{'name':{'type':'string'},'optional_count':{'type':'integer','default':3}},
            'required':['name'] if required is None else required}}
        self.bot.on_server_request({'id':2,'method':'mcpServer/elicitation/request','params':params})
        return params, next(iter(self.bot.pending_questions))

    def test_old_turn_choice_cannot_steer_new_turn(self):
        self.choice()
        with self.assertRaises(ValueError): self.bot._direct_reply_choice_inner('seq:steer')
        self.bot.app.request.assert_not_called()

    def test_finished_turn_choice_needs_new_user_intent(self):
        self.choice(current=None)
        self.bot.start_turn = Mock()
        with self.assertRaises(ValueError): self.bot._direct_reply_choice_inner('seq:steer')
        self.bot.start_turn.assert_not_called(); self.bot.app.request.assert_not_called()

    def test_same_turn_choice_steers_only_once(self):
        self.choice(current='old'); self.bot._direct_reply_choice_inner('seq:steer')
        self.assertEqual(self.bot.app.request.call_args.args[1]['expectedTurnId'],'old')
        with self.assertRaises(ValueError): self.bot._direct_reply_choice_inner('seq:steer')
        self.bot.app.request.assert_called_once()

    def test_desktop_steer_keeps_expected_turn(self):
        self.bot.app.request.return_value={'thread':{'id':'external','cwd':str(self.root)}}
        self.bot._desktop_follow_up=Mock(return_value={'turn_id':'old'})
        self.bot.steer_turn('external','intent',expected_turn_id='old')
        self.assertEqual(self.bot._desktop_follow_up.call_args.kwargs['expected_turn_id'],'old')

    def test_unknown_steer_is_not_replayed(self):
        self.choice(current='old');self.bot.app.request.side_effect=b.AppServerSubmissionUnknown('unknown')
        self.bot.start_turn=Mock()
        with self.assertRaises(b.AppServerSubmissionUnknown):self.bot._direct_reply_choice_inner('seq:steer')
        self.bot.app.request.assert_called_once();self.bot.start_turn.assert_not_called()

    def test_old_question_button_does_not_answer_next_question(self):
        seq=self.question()
        first=self.bot.enqueue.call_args.args[1]['inline_keyboard'][0][0]['callback_data'].split('|',1)[1]
        self.bot._handle_question_callback(first)
        with self.assertRaises(ValueError): self.bot._handle_question_callback(first)
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['A1']}})
        self.bot.app.answer.assert_not_called()

    def test_normal_question_callbacks_finish_in_order(self):
        seq=self.question()
        self.bot._handle_question_callback(seq+':0:0')
        self.bot._handle_question_callback(seq+':1:0')
        self.bot.app.answer.assert_called_once_with(1,{'answers':{'q1':{'answers':['A1']},'q2':{'answers':['A2']}}})

    def test_legacy_question_callback_fails_closed(self):
        seq=self.question()
        with self.assertRaises(ValueError):self.bot._handle_question_callback(seq+':0')
        self.assertEqual(self.bot.pending_questions[seq]['index'],0)

    def test_delayed_free_text_cannot_answer_next_question(self):
        seq=self.question()
        self.bot._handle_question_callback(seq+':0:other')
        self.bot._handle_question_callback(seq+':0:0')
        try:self.bot.handle_text('late q1 text')
        except ValueError:pass
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['A1']}})
        self.bot.app.answer.assert_not_called()

    def test_cancel_one_prompt_keeps_sibling_and_persists_cancellation(self):
        self.bot.awaiting_direct_reply={101:'task',102:'task',103:'other'}
        n.remember_task_message(self.bot.token,{'chat_id':'123','reply_markup':{'inline_keyboard':[
            [{'text':'reply','callback_data':'direct|task'}]]}},{'message_id':101})
        self.bot.cancel_direct_reply('task',message_id=101)
        self.assertEqual(self.bot.awaiting_direct_reply,{102:'task',103:'other'})
        self.bot.state.reload()
        self.bot.submit_direct_reply=Mock()
        self.bot.handle_text('cancelled',reply_to_message_id=101)
        self.bot.submit_direct_reply.assert_not_called()
        self.bot.handle_text('sibling',reply_to_message_id=102)
        self.bot.submit_direct_reply.assert_called_once_with('task','sibling')

    def test_ambiguous_legacy_cancel_cannot_cancel_all_prompts(self):
        self.bot.awaiting_direct_reply={101:'task',102:'task'}
        with self.assertRaises(ValueError):self.bot.cancel_direct_reply('task')
        self.assertEqual(len(self.bot.awaiting_direct_reply),2)

    def test_callback_cancel_targets_clicked_message(self):
        self.bot.awaiting_direct_reply={101:'task',102:'task'}
        self.bot._handle_callback_inner({'data':'directcancel|task','message':{'message_id':101,'chat':{'id':123}}})
        self.assertEqual(self.bot.awaiting_direct_reply,{102:'task'})

    def test_cancel_old_persisted_prompt_after_restart(self):
        n.remember_task_message(self.bot.token,{'chat_id':'123','reply_markup':{'inline_keyboard':[
            [{'text':'reply','callback_data':'direct|task'}]]}},{'message_id':101})
        self.bot.cancel_direct_reply('task',message_id=101)
        self.bot.submit_direct_reply=Mock()
        self.bot.handle_text('cancelled',reply_to_message_id=101)
        self.bot.submit_direct_reply.assert_not_called()

    def test_explicit_prompt_reopens_cancelled_message_and_keeps_other_tombstones(self):
        self.bot.awaiting_direct_reply.update({101:'task',102:'task'})
        self.bot.cancel_direct_reply('task',101)
        self.bot.cancel_direct_reply('task',102)
        self.bot.request_direct_reply('task')
        key=f'{self.bot.token.split(":")[0]}:{self.bot.chat_id}:101'
        sibling=f'{self.bot.token.split(":")[0]}:{self.bot.chat_id}:102'
        self.assertNotIn(key,self.bot.state.snapshot()['cancelled_direct_prompts'])
        self.assertIn(sibling,self.bot.state.snapshot()['cancelled_direct_prompts'])
        self.bot.submit_direct_reply=Mock()
        self.bot.handle_text('fresh response',reply_to_message_id=101)
        self.bot.submit_direct_reply.assert_called_once_with('task','fresh response')

    def test_late_free_text_binding_is_checked_even_if_question_advanced(self):
        seq=self.question()
        self.bot.pending_free_text[self.bot.chat_id]=seq+':0'
        self.bot.pending_questions[seq]['index']=1
        self.bot.handle_text('late answer')
        self.bot._input_error.assert_called_once()
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{})

    def test_valid_free_text_is_bound_to_selected_question(self):
        seq=self.question()
        self.bot._handle_question_callback(seq+':0:other')
        self.bot.handle_text('first free answer',reply_to_message_id=101)
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['first free answer']}})
        self.assertNotIn(self.bot.chat_id,self.bot.pending_free_text)

    def test_late_unquoted_first_answer_cannot_fill_armed_second_question(self):
        seq=self.question()
        self.bot.send.side_effect=[{'message_id':101},{'message_id':102}]
        self.bot._handle_question_callback(seq+':0:other')
        self.bot._handle_question_callback(seq+':0:0')
        self.bot._handle_question_callback(seq+':1:other')
        self.bot.handle_text('late first answer')
        self.bot.handle_text('late first quoted answer',reply_to_message_id=101)
        self.bot.app.answer.assert_not_called()
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['A1']}})
        self.bot.handle_text('second answer',reply_to_message_id=102)
        self.bot.app.answer.assert_called_once_with(1,{'answers':{'q1':{'answers':['A1']},'q2':{'answers':['second answer']}}})

    def test_single_question_text_prompt_is_new_message_without_force_reply_and_requires_quote(self):
        self.bot.on_server_request({'id':3,'method':'item/tool/requestUserInput','params':{
            'threadId':'task','questions':[{'id':'only','question':'Your answer?'}]}})
        seq=next(iter(self.bot.pending_questions));self.bot._ui_local.message_id=77
        self.bot.send.side_effect=[{'message_id':77},{'message_id':101}]
        self.bot._handle_question_callback(seq+':0:other')
        markup=self.bot.send.call_args.args[1]
        self.assertNotIn('force_reply',markup)
        self.assertEqual(markup['inline_keyboard'][0][0]['callback_data'],f'canceltext|{seq}:0')
        self.assertEqual(self.bot._ui_local.message_id,77)
        self.bot.send.side_effect=None
        self.bot._ui_local.message_id=None
        self.bot.handle_text('no quote')
        self.bot.app.answer.assert_not_called()
        self.bot.handle_text('quoted answer',reply_to_message_id=101)
        self.bot.app.answer.assert_called_once_with(3,{'answers':{'only':{'answers':['quoted answer']}}})

    def test_cancel_free_text_prompt_invalidates_its_reply(self):
        seq=self.question();self.bot._handle_question_callback(seq+':0:other')
        self.bot._handle_callback_inner({'data':f'canceltext|{seq}:0','message':{'message_id':77,'chat':{'id':123}}})
        self.bot.handle_text('cancelled text',reply_to_message_id=101)
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{})
        self.bot.app.answer.assert_not_called()

    def test_form_optional_fields_can_be_omitted_without_inventing_default(self):
        params,seq=self.form()
        self.assertFalse(form_questions(params)[1]['required'])
        self.assertEqual(form_content(params,{'name':{'answers':['Alice']}}),{'name':'Alice'})
        self.bot._answer_question(seq,'Alice')
        markup=self.bot.enqueue.call_args.args[1]
        self.assertTrue(any(c['callback_data']==f'question|{seq}:1:skip' for row in markup['inline_keyboard'] for c in row))
        self.bot._handle_question_callback(seq+':1:skip')
        self.bot.app.answer.assert_called_once_with(2,{'action':'accept','content':{'name':'Alice'}})

    def test_required_form_field_cannot_be_skipped(self):
        params,seq=self.form()
        with self.assertRaises(ValueError):self.bot._handle_question_callback(seq+':0:skip')
        with self.assertRaises(ValueError):form_content(params,{})
        self.assertEqual(self.bot.pending_questions[seq]['index'],0)

    def test_optional_typed_values_keep_validation(self):
        for kind,raw,want in [('string','hello','hello'),('integer','3',3),('boolean','false',False)]:
            params={'requestedSchema':{'properties':{'value':{'type':kind}},'required':[]}}
            self.assertEqual(form_content(params,{}),{})
            self.assertEqual(form_content(params,{'value':{'answers':[raw]}}),{'value':want})
        params={'requestedSchema':{'properties':{'value':{'type':'integer','minimum':2,'enum':[2,3]}}}}
        with self.assertRaises(ValueError):form_content(params,{'value':{'answers':['1']}})


class QuestionTextDeliveryTests(unittest.TestCase):
    setUp = delivery_fixture.ControllerDeliveryTests.setUp
    migrate = delivery_fixture.ControllerDeliveryTests.migrate
    owned = delivery_fixture.ControllerDeliveryTests.owned
    update = delivery_fixture.ControllerDeliveryTests.update
    ready = delivery_fixture.ControllerDeliveryTests.ready

    def question(self):
        self.owned()
        self.bot.on_server_request({'id':80,'method':'item/tool/requestUserInput','params':{
            'threadId':'owned-thread','turnId':'turn-one','questions':[
                {'id':'q1','question':'First?','options':[{'label':'A'}]},
                {'id':'q2','question':'Second?','options':[{'label':'B'}]}]}})
        self.bot.delivery.tick()
        return next(iter(self.bot.pending_questions))

    def test_question_callback_sends_text_prompt_without_force_reply_as_new_message(self):
        seq=self.question()
        self.bot._handle_callback_scoped({'id':'question-text','data':f'question|{seq}:0:other',
            'message':{'message_id':100,'chat':{'id':42,'type':'private'}}})
        self.assertFalse(any(payload.get('reply_markup',{}).get('force_reply') for _,payload in self.calls))
        message=self.bot.pending_questions[seq]['free_text_prompt']['message_id']
        self.assertNotEqual(message,100)
        prompts=[payload for method,payload in self.calls if method=='sendRichMessage']
        self.assertEqual(prompts[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'],f'canceltext|{seq}:0')
        self.bot.handle_text('explicit answer',reply_to_message_id=message)
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['explicit answer']}})

    def test_help_clears_legacy_keyboard_with_both_delivery_paths(self):
        delivery=self.bot.delivery
        for transport in (delivery,None):
            with self.subTest(durable=transport is not None):
                self.calls.clear()
                self.bot.delivery=transport
                self.bot.command_help()
                messages=[payload for method,payload in self.calls if method=='sendRichMessage']
                self.assertEqual(len(messages),1)
                self.assertEqual(messages[0]['reply_markup'],{'remove_keyboard':True})
                self.assertNotIn('message_id',messages[0])
                self.rpc.request.assert_not_called()

    def test_legacy_transport_text_prompt_never_forces_reply(self):
        seq=self.question()
        self.bot.delivery=None
        self.calls.clear()
        self.bot._handle_question_callback(seq+':0:other')
        messages=[payload for method,payload in self.calls if method=='sendRichMessage']
        self.assertEqual(len(messages),1)
        self.assertNotIn('force_reply',messages[0]['reply_markup'])
        self.assertEqual(messages[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'],f'canceltext|{seq}:0')
        message=self.bot.pending_questions[seq]['free_text_prompt']['message_id']
        self.bot.handle_text('legacy answer',reply_to_message_id=message)
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['legacy answer']}})

    def test_delayed_explicit_prompt_clears_only_matching_cancelled_message(self):
        self.bot.awaiting_direct_reply.update({101:'original-thread',102:'original-thread'})
        self.bot.cancel_direct_reply('original-thread',101)
        self.bot.cancel_direct_reply('original-thread',102)
        self.bot._ui_local.message_id=101
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot.request_direct_reply('original-thread')
        key=f'{self.bot.token.split(":")[0]}:{self.bot.chat_id}:101'
        sibling=f'{self.bot.token.split(":")[0]}:{self.bot.chat_id}:102'
        self.assertIn(key,self.bot.state.snapshot()['cancelled_direct_prompts'])
        self.assertNotIn(101,self.bot.awaiting_direct_reply)
        self.bot._ui_local.message_id=None
        self.ready();self.bot.delivery.tick()
        self.assertNotIn(key,self.bot.state.snapshot()['cancelled_direct_prompts'])
        self.assertIn(sibling,self.bot.state.snapshot()['cancelled_direct_prompts'])
        self.bot.submit_direct_reply=Mock()
        self.bot.handle_text('new response',reply_to_message_id=101)
        self.bot.submit_direct_reply.assert_called_once_with('original-thread','new response')

    def test_429_prompt_binds_only_after_delivery_and_quote_answers_correct_question(self):
        seq=self.question()
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot._handle_question_callback(seq+':0:other')
        prompt=self.bot.pending_questions[seq]['free_text_prompt']
        self.assertNotIn('message_id',prompt)
        self.bot.handle_text('unquoted before delivery')
        self.rpc.answer.assert_not_called()
        self.ready();self.bot.delivery.tick()
        message=self.bot.pending_questions[seq]['free_text_prompt']['message_id']
        self.bot.handle_text('first',reply_to_message_id=message)
        self.assertEqual(self.bot.pending_questions[seq]['answers'],{'q1':{'answers':['first']}})
        self.bot._handle_question_callback(seq+':1:other')
        second=self.bot.pending_questions[seq]['free_text_prompt']['message_id']
        self.bot.handle_text('stale first',reply_to_message_id=message)
        self.rpc.answer.assert_not_called()
        self.bot.handle_text('second',reply_to_message_id=second)
        self.rpc.answer.assert_called_once_with(80,{'answers':{'q1':{'answers':['first']},'q2':{'answers':['second']}}})
        self.assertFalse(any(payload.get('reply_markup',{}).get('force_reply') for _,payload in self.calls))

    def test_queued_text_prompt_expires_when_question_advances(self):
        seq=self.question()
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot._handle_question_callback(seq+':0:other')
        row=self.legacy.db.execute("SELECT id FROM outbox WHERE json_extract(route,'$.after[0]')='question_text'").fetchone()
        self.bot._handle_question_callback(seq+':0:0')
        self.ready();self.bot.delivery.tick()
        self.assertEqual(self.legacy.message_status(row[0]),'expired')
        self.assertNotIn('free_text_prompt',self.bot.pending_questions[seq])

    def test_queued_text_prompt_is_invalid_after_service_restart(self):
        seq=self.question()
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot._handle_question_callback(seq+':0:other')
        row=self.legacy.db.execute("SELECT id FROM outbox WHERE json_extract(route,'$.after[0]')='question_text'").fetchone()
        self.bot.pending_questions.clear()
        self.ready();self.bot.delivery.tick()
        self.assertEqual(self.legacy.message_status(row[0]),'expired')

    def test_reopening_same_question_invalidates_first_queued_prompt(self):
        seq=self.question()
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot._handle_question_callback(seq+':0:other')
        first=self.legacy.db.execute("SELECT id FROM outbox WHERE json_extract(route,'$.after[0]')='question_text'").fetchone()[0]
        self.bot._handle_question_callback(seq+':0:other')
        self.ready();self.bot.delivery.tick()
        self.assertEqual(self.legacy.message_status(first),'expired')
        self.bot.delivery.tick()
        self.assertIsInstance(self.bot.pending_questions[seq]['free_text_prompt']['message_id'],int)

    def test_cancel_text_input_expires_queued_prompt_without_cancelling_question(self):
        seq=self.question()
        self.delivery_error=n.TelegramApiError(429,'retry after 5')
        self.bot._handle_question_callback(seq+':0:other')
        row=self.legacy.db.execute("SELECT id FROM outbox WHERE json_extract(route,'$.after[0]')='question_text'").fetchone()
        self.bot._handle_callback_inner({'data':f'canceltext|{seq}:0','message':{'message_id':77,'chat':{'id':42}}})
        self.ready();self.bot.delivery.tick()
        self.assertEqual(self.legacy.message_status(row[0]),'expired')
        self.assertIn(seq,self.bot.pending_questions)
