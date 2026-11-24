export type ConferenceStep = 'materials' | 'talks' | 'screening' | 'review' | 'report';
export type Workflow = 'operator' | 'legacy';

export type ConferenceItem = {
  id: string;
  title: string | null;
  start_date: string | null;
  end_date: string | null;
  place: string | null;
  organizer: string | null;
  processing_started: boolean;
  workflow?: Workflow;
  resume_step?: ConferenceStep;
  talks: number;
  summaries: number;
  reviewed: number;
};

