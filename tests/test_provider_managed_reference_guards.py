"""Independent synthetic SQL controls and fail-closed reference probes."""
import sqlite3,json
from bots5.infrastructure.persistence.provider_managed_schema import reference_guards
SCHEMA='''
CREATE TABLE generation_attempts(id,user_message_id,request_snapshot);
CREATE TABLE context_plans(attempt_id,canonical_representation);
CREATE TABLE provider_managed_context_plans(attempt_id,canonical_representation);
CREATE TABLE attachments(id,blob_digest);
CREATE TABLE attachment_blobs(digest,state);
CREATE TABLE message_attachments(message_id,attachment_id,ordinal);
CREATE TABLE attempt_attachments(attempt_id,attachment_id,ordinal);
CREATE TABLE archive_continuation_branches(attempt_id,chat_id,base_key,choice_revision);
CREATE TABLE archive_continuation_choices(chat_id,base_key,choice_revision,excluded_refs);
CREATE TABLE archive_continuation_requirement_candidates(chat_id,base_key,ordinal,imported_ref_id);
CREATE TABLE archive_import_attachment_refs(id,attachment_id,availability);
CREATE TABLE archive_continuation_requirements(chat_id,base_key,ordinal);
'''
def check(kind,mutation):
 db=sqlite3.connect(':memory:');db.executescript(SCHEMA)
 snap={'snapshot_version':5,'accounting_mode':'provider-managed'}
 if mutation=='wrong_mode':snap['accounting_mode']='exact'
 if mutation=='wrong_version':snap['snapshot_version']=3
 sources=[dict(kind='attachment',source_id='att',selected=mutation!='excluded')]
 db.execute('INSERT INTO generation_attempts VALUES(?,?,?)',('a','m',json.dumps(snap)))
 if mutation!='missing_plan':db.execute('INSERT INTO provider_managed_context_plans VALUES(?,?)',('a',json.dumps({'sources':sources})))
 db.execute('INSERT INTO attachments VALUES(?,?)',('att','blob'));db.execute('INSERT INTO attachment_blobs VALUES(?,?)',('blob','staged' if mutation=='not_ready' else 'ready'))
 if mutation!='missing_owner':db.execute('INSERT INTO message_attachments VALUES(?,?,?)',('m','att',0))
 for statement in reference_guards().values():db.execute(statement)
 table='message_attachments' if kind=='message' else 'attempt_attachments'
 identity=('m' if kind=='message' else 'a') if mutation!='wrong_owner' else 'different'
 try:db.execute('INSERT INTO '+table+' VALUES(?,?,?)',(identity,'unknown' if mutation=='wrong_attachment' else 'att',1 if mutation=='wrong_ordinal' else 0));accepted=True
 except sqlite3.IntegrityError:accepted=False
 expected=mutation=='valid' or (kind=='message' and mutation=='missing_owner')
 assert accepted==expected,(kind,mutation,accepted)
 return int(not accepted)

def test_provider_managed_sql_attachment_reference_guards():
    rejected = 0
    for kind in ('message','attempt'):
        for mutation in ('valid','wrong_mode','wrong_version','missing_plan','not_ready','excluded','wrong_owner','wrong_attachment','wrong_ordinal','missing_owner'):
            rejected += check(kind, mutation)
    assert rejected == 17
