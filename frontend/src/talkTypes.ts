export type Screening = {
  version: number;
  state: string;
  eligible: boolean;
  stale: boolean;
  manual: boolean;
  error: string | null;
  department_ids: string[];
  items: { department_id: string; department_name: string; status: string; reason: string; evidence: { label: string; quote: string }[]; paragraphs: { label: string; quote: string }[] }[];
};
type Job = 'queued' | 'running' | 'done' | 'failed';
export type TalkProgress = {
  id: string;
  section: string;
  order: number;
  title: string | null;
  status: string;
  job: Job | null;
  step: 'screening' | 'parse' | 'facts' | 'verify' | 'summary' | null;
  error: string | null;
  screening?: Screening;
  summary_number: number | null;
  bulk_reviewable?: boolean;
  summary_status: 'ready' | 'needs_attention' | 'stale' | null;
};
export type Progress = {
  processing_started: boolean;
  files: number;
  talks: TalkProgress[];
};

