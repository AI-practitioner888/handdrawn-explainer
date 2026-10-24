"""Behavior checks for retained objects, stale reviews and matching speech."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from revise_project import apply_patch, json_bytes, sha256_bytes, PATCH_SCHEMA

class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='handdrawn-revision-')
        self.root = Path(self.folder.name)
        self.doc={'durationMs':2000,'fps':30,'canvas':{'width':1280,'height':720},
                  'drawPolicy':{'profile':'legacy'},
                  'narrationBeats':[{'id':'first','startMs':0,'endMs':2000}],
                  'scenes':[{'id':'a','startMs':0,'endMs':2000,'narrationText':'old words',
                    'elements':[{'id':'title','type':'text','text':'Old title','x':640,'y':40,'size':40,
                                 'instant':True,'transform':{'x':0,'y':0,'scale':1},'speechBeat':'first'},
                                {'id':'kept','type':'stroke','d':'M 10 20 L 30 40','color':'#123456',
                                 'instant':True}]}]}
        self.plan={'durationMs':2000,'mode':'files','segments':[{
            'id':'voice','startMs':0,'endMs':2000,'text':'Old words','spokenText':'Old pronunciation',
            'audioFile':'old.wav','trimSilence':False,'provenance':{'provider':'edge-tts'}}]}
        self.project_hash=sha256_bytes(json_bytes(self.doc))
        self.plan_hash=sha256_bytes(json_bytes(self.plan))
    def tearDown(self):self.folder.cleanup()
    def patch(self,operation):
        return {'schemaVersion':PATCH_SCHEMA,'baseProjectSha256':self.project_hash,
                'baseAudioPlanSha256':self.plan_hash,'operations':[operation]}
    def apply(self,patch,plan=True):
        return apply_patch(self.doc,patch,self.root/'project.json',
                           self.plan if plan else None,self.root/'narration.json' if plan else None,
                           self.project_hash,self.plan_hash if plan else None)
    def test_local_visual_edit_preserves_voice_and_other_object(self):
        original=copy.deepcopy(self.doc)
        result,audio,changes=self.apply(self.patch({'target':'visual','sceneId':'a','elementId':'title',
                                                   'set':{'text':'New title','transform':{'x':20}}}))
        self.assertEqual(result['scenes'][0]['elements'][1],original['scenes'][0]['elements'][1])
        self.assertEqual(audio,self.plan)
        self.assertEqual(result['scenes'][0]['elements'][0]['transform'],{'x':20,'y':0,'scale':1})
        self.assertEqual(self.doc,original)
        self.assertEqual(len(changes),1)
    def test_stale_unknown_and_restricted_refused(self):
        patch=self.patch({'target':'visual','sceneId':'a','elementId':'title','set':{'text':'New'}})
        cases=[]
        stale=copy.deepcopy(patch);stale['baseProjectSha256']='old';cases.append(stale)
        unknown=copy.deepcopy(patch);unknown['operations'][0]['elementId']='missing';cases.append(unknown)
        restricted=copy.deepcopy(patch);restricted['operations'][0]['set']={'id':'renamed'};cases.append(restricted)
        before=json_bytes(self.doc)
        for bad in cases:
            with self.assertRaises(ValueError):self.apply(bad)
            self.assertEqual(json_bytes(self.doc),before)
    def test_generated_speech_invalidates_old_recording_and_markers(self):
        result,audio,changes=self.apply(self.patch({'target':'speech','sceneId':'a','segmentId':'voice',
                                                   'set':{'text':'New words'}}))
        segment=audio['segments'][0]
        self.assertNotIn('audioFile',segment)
        self.assertNotIn('spokenText',segment)
        self.assertTrue(segment['trimSilence'])
        self.assertEqual(audio['mode'],'auto')
        self.assertEqual(result['narrationTimingStatus'],'needs-realignment')
        self.assertNotIn('narrationBeats',result)
        self.assertNotIn('narrationText',result['scenes'][0])
        self.assertNotIn('speechBeat',result['scenes'][0]['elements'][0])
    def test_imported_voice_requires_matching_replacement_decision(self):
        self.plan['segments'][0].pop('provenance')
        op={'target':'speech','sceneId':'a','segmentId':'voice','set':{'text':'New words'}}
        with self.assertRaises(ValueError):self.apply(self.patch(op))
        op['regenerate']=True
        _,audio,_=self.apply(self.patch(op))
        self.assertNotIn('audioFile',audio['segments'][0])
    def test_stale_audio_plan_and_duplicate_edit_refused(self):
        op={'target':'speech','sceneId':'a','segmentId':'voice','set':{'text':'New words'}}
        patch=self.patch(op);patch['baseAudioPlanSha256']='old'
        with self.assertRaises(ValueError):self.apply(patch)
        patch=self.patch(op);patch['operations'].append(copy.deepcopy(op))
        with self.assertRaises(ValueError):self.apply(patch)

if __name__=='__main__':unittest.main()
