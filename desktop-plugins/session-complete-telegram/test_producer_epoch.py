"""Real notifier producer/registration epoch boundary, synthetic send only."""
import copy,time,unittest
from unittest.mock import patch
import notifier,test_notifier
class ProducerEpochTests(unittest.TestCase):
 def setUp(self):
  self.f=test_notifier.NotificationTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.addCleanup(self.f.doCleanups)
  (self.f.home/'config.yaml').write_text('plugins:\n  enabled: [session-complete-telegram]\n  entries:\n    session-complete-telegram:\n      settings:\n        replies_enabled: true\n',encoding='utf-8')
  self.bridge=notifier._sibling_module('followup_bridge');self.bridge.bind_receiver(77,99,None,'producer-boot',private_verified=True,state=self.f.state);self.bridge.heartbeat_owner('producer-owner',state=self.f.state)
  self.req=self.f.request();self.checked=self.f.inspect(self.req);self.assertTrue(self.checked['ok']);self.req['followup']={'owner':'producer-owner','connectionId':'local','profile':'default','targetProfile':'default','sid':'runtime-one','session_key':self.req['session_key'],'epoch':'one','started_at':'one','final_row_id':self.checked['final_row_id']}
 def run_send(self,retire=False):
  events=[];capture=self.bridge.capture_registration_epoch
  def cap(**kw):events.append('capture');return capture(**kw)
  def send(*args,**kw):
   self.assertEqual(events,['capture'],'capture must precede network transmission')
   events.append('send')
   if retire:self.bridge.retire_unexecuted(state=self.f.state)
   return {'ok':True,'status':'sent','verified':True,'followup_verified':True,'message_id':901}
  with patch.object(self.bridge,'capture_registration_epoch',side_effect=cap),patch.object(notifier,'credentials',return_value=('77:synthetic-token',99,None)),patch.object(notifier,'_send_verified',side_effect=send):
   return notifier.deliver(copy.deepcopy(self.req),root=self.f.root,state=self.f.state)
 def test_capture_before_send_creates_fresh_binding(self):
  result=self.run_send();self.assertIn('binding_id',result)
 def test_off_on_epoch_during_send_leaves_known_reject_only_card(self):
  result=self.run_send(retire=True);self.assertNotIn('binding_id',result);out=self.bridge.lookup_outbound(77,99,901,state=self.f.state);self.assertEqual(out,{'kind':'legacy','binding':None})
 def test_new_work_after_prior_revocation_uses_fresh_epoch(self):
  self.bridge.retire_unexecuted(state=self.f.state);result=self.run_send();self.assertIn('binding_id',result)
if __name__=='__main__':unittest.main(verbosity=2)
