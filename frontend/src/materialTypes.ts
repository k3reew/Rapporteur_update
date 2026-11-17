export type Conference = { id: string; title: string | null };
export type Section = { id: string; title: string };
export type Talk = {
  id: string;
  section_id: string;
  section: string;
  order: number;
  title: string | null;
  status: string;
};
export type Material = {
  source: {
    id: string;
    kind: 'presentation' | 'audio' | 'video' | 'notes' | 'program' | 'document' | 'image';
    encoding: string | null;
    duration_ms: number | null;
  };
  relative_path: string;
  title_candidates: { title: string | null; speakers: string[] } | null;
  warning: string | null;
  binding: { confirmed: boolean; talk_id: string | null };
  suggested_talk_id: string | null;
  confidence: number | null;
  alternate_of: string | null;
  range_talk_ids?: string[];
};
export type Review = { processing_started: boolean; files: Material[]; sections: Section[]; talks: Talk[] };
