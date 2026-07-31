import { NextResponse } from 'next/server';
import { AgentDispatchClient } from 'livekit-server-sdk';

/**
 * Wake the agent before the caller needs it.
 *
 * On LiveKit Cloud's free plan a deployed agent is shut down once its sessions
 * end, and the next call pays a 10-20 second cold start before the agent joins.
 * The page calls this on load, so the agent boots while the caller is still
 * reading the page and granting microphone access rather than sitting in silence
 * after pressing Call.
 *
 * It dispatches to a throwaway room the caller never joins. The agent starts,
 * finds nobody there, and the empty room is reaped on its own.
 */
export const revalidate = 0;

export async function POST() {
  const url = process.env.LIVEKIT_URL;
  const key = process.env.LIVEKIT_API_KEY;
  const secret = process.env.LIVEKIT_API_SECRET;
  const agentName = process.env.AGENT_NAME;

  // Without an agent name there is no explicit dispatch to make, and nothing to warm.
  if (!url || !key || !secret || !agentName) {
    return NextResponse.json({ warmed: false, reason: 'not configured' });
  }

  try {
    const client = new AgentDispatchClient(url.replace(/^wss:/, 'https:'), key, secret);
    await client.createDispatch(`warmup_${Date.now()}`, agentName);
    return NextResponse.json({ warmed: true });
  } catch (error) {
    // Warming is an optimisation. If it fails the call still works, just slower,
    // so this must never surface as an error to the caller.
    console.warn('agent warmup failed', error);
    return NextResponse.json({ warmed: false, reason: 'dispatch failed' });
  }
}
