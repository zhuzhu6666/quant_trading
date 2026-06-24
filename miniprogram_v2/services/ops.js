import { get } from './client';
import systemStore from '../stores/system';

export async function refreshOpsDomain() {
  const [scheduler, evolution, dbHealth, apiHealth] = await Promise.all([
    get('/api/control/scheduler').catch(() => null),
    get('/api/control/evolution/latest').catch(() => null),
    get('/api/system/db-health').catch(() => null),
    get('/api/health').catch(() => null),
  ]);
  systemStore.setState({
    scheduler: scheduler || null,
    evolution: evolution || null,
    dbHealth: dbHealth || null,
    apiHealth: apiHealth || null,
    updatedAt: Date.now(),
  });
  return systemStore.getState();
}
