import { get } from './client';
import systemStore from '../stores/system';

export async function refreshFactorDomain() {
  const [factorStats, factorWeights, factorHealth] = await Promise.all([
    get('/api/v4/stats').catch(() => null),
    get('/api/v4/weights').catch(() => []),
    get('/api/factor-health/latest').catch(() => null),
  ]);
  systemStore.setState({
    factorStats: factorStats || null,
    factorWeights: factorWeights || [],
    factorHealth: factorHealth || null,
    updatedAt: Date.now(),
  });
  return systemStore.getState();
}
