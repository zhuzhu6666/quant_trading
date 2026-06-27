import { createStore } from '../utils/store';

const store = createStore({
  summary: null,
  summaryStatus: 'idle',
  summaryError: '',
  metaLightgbmShadowReport: null,
  metaLightgbmShadowReportView: null,
  metaLightgbmShadowReportStatus: 'idle',
  metaLightgbmShadowReportError: '',
  metaLightgbmShadowReportSnapshots: [],
  metaLightgbmShadowReportSnapshotsView: null,
  metaLightgbmShadowReportSnapshotsStatus: 'idle',
  metaLightgbmShadowReportSnapshotsError: '',
  offmarketHighLoadAudits: [],
  offmarketHighLoadAuditsView: null,
  offmarketHighLoadAuditsStatus: 'idle',
  offmarketHighLoadAuditsError: '',
  suggestions: [],
  applications: [],
  reviews: [],
  lifecycle: [],
  offlineCandidates: [],
  templateRecommendations: [],
  pendingGovernanceFocus: null,
  updatedAt: 0,
});

export default store;
