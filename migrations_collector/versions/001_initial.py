"""Immutable baseline for the standalone collector database."""
revision = "001"
down_revision = None

DDL = [
    'CREATE TABLE collection_admin (\n\tid INTEGER NOT NULL, \n\tpassword_hash VARCHAR NOT NULL, \n\tPRIMARY KEY (id)\n)',
    'CREATE TABLE collection_events (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR, \n\tjob_id VARCHAR, \n\tcode VARCHAR NOT NULL, \n\tmessage VARCHAR NOT NULL, \n\tcreated_at FLOAT, \n\tresolved_at FLOAT, \n\tPRIMARY KEY (id)\n)',
    'CREATE TABLE source_accounts (\n\tid VARCHAR NOT NULL, \n\tprovider VARCHAR NOT NULL, \n\tstate VARCHAR NOT NULL, \n\treason VARCHAR, \n\tpaused_until FLOAT, \n\tlast_request_at FLOAT, \n\tlast_auth_at FLOAT, \n\tnetwork_failures INTEGER, \n\tPRIMARY KEY (id)\n)',
    'CREATE TABLE feeds (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\texternal_id VARCHAR NOT NULL, \n\tname VARCHAR NOT NULL, \n\tenabled BOOLEAN, \n\tinterval_minutes INTEGER, \n\tnext_run_at FLOAT, \n\tlast_check_at FLOAT, \n\tlast_new_at FLOAT, \n\thistory_cursor INTEGER, \n\tcoverage VARCHAR, \n\tarchive_images BOOLEAN, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, external_id), \n\tFOREIGN KEY(source_id) REFERENCES source_accounts (id)\n)',
    'CREATE TABLE articles (\n\tid VARCHAR NOT NULL, \n\tfeed_id VARCHAR NOT NULL, \n\tsource_article_id VARCHAR NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\turl TEXT NOT NULL, \n\tcanonical_url TEXT NOT NULL, \n\tauthor VARCHAR, \n\tpublished_at FLOAT, \n\tdiscovered_at FLOAT, \n\tcontent_status VARCHAR, \n\tmedia_status VARCHAR, \n\tcontent_html TEXT, \n\tplain_text TEXT, \n\tcontent_hash VARCHAR, \n\tlast_success_at FLOAT, \n\tlast_error VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (feed_id, source_article_id), \n\tFOREIGN KEY(feed_id) REFERENCES feeds (id)\n)',
    'CREATE INDEX ix_articles_feed_id ON articles (feed_id)',
    'CREATE INDEX ix_articles_published_at ON articles (published_at)',
    'CREATE TABLE article_assets (\n\tid INTEGER NOT NULL, \n\tarticle_id VARCHAR NOT NULL, \n\tsource_url TEXT NOT NULL, \n\tlocal_path VARCHAR, \n\tstatus VARCHAR, \n\terror_code VARCHAR, \n\tPRIMARY KEY (id), \n\tUNIQUE (article_id, source_url), \n\tFOREIGN KEY(article_id) REFERENCES articles (id)\n)',
    'CREATE TABLE article_versions (\n\tid VARCHAR NOT NULL, \n\tarticle_id VARCHAR NOT NULL, \n\tcontent_hash VARCHAR NOT NULL, \n\traw_path VARCHAR NOT NULL, \n\tclean_path VARCHAR NOT NULL, \n\tcaptured_at FLOAT, \n\tPRIMARY KEY (id), \n\tUNIQUE (article_id, content_hash), \n\tFOREIGN KEY(article_id) REFERENCES articles (id)\n)',
    'CREATE TABLE collection_jobs (\n\tid VARCHAR NOT NULL, \n\tsource_id VARCHAR NOT NULL, \n\tfeed_id VARCHAR, \n\tarticle_id VARCHAR, \n\tkind VARCHAR NOT NULL, \n\tstatus VARCHAR, \n\tpriority INTEGER, \n\tidempotency_key VARCHAR NOT NULL, \n\tactive_key VARCHAR, \n\tpayload JSON, \n\tresult JSON, \n\tattempts INTEGER, \n\tcreated_at FLOAT, \n\trun_after FLOAT, \n\tstarted_at FLOAT, \n\tended_at FLOAT, \n\tlease_until FLOAT, \n\tlease_token VARCHAR, \n\theartbeat_at FLOAT, \n\tcancel_requested BOOLEAN, \n\terror_code VARCHAR, \n\terror_message VARCHAR, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES source_accounts (id), \n\tFOREIGN KEY(feed_id) REFERENCES feeds (id), \n\tFOREIGN KEY(article_id) REFERENCES articles (id), \n\tUNIQUE (idempotency_key), \n\tUNIQUE (active_key)\n)',
    'CREATE INDEX ix_collection_jobs_run_after ON collection_jobs (run_after)',
    'CREATE INDEX ix_collection_jobs_status ON collection_jobs (status)',
    'CREATE TABLE collection_attempts (\n\tid INTEGER NOT NULL, \n\tjob_id VARCHAR NOT NULL, \n\tnumber INTEGER, \n\tstarted_at FLOAT, \n\tended_at FLOAT, \n\toutcome VARCHAR, \n\terror_code VARCHAR, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(job_id) REFERENCES collection_jobs (id)\n)',
    'CREATE INDEX ix_collection_attempts_job_id ON collection_attempts (job_id)',
 ]

def upgrade():
    from alembic import op
    for statement in DDL:
        op.execute(statement)

def downgrade():
    raise RuntimeError("Restore a pre-migration backup instead of dropping archives.")
