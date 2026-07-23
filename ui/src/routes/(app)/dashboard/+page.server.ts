import { redirect } from '@sveltejs/kit';
// Stripped route — bounce to its successor in the new IA.
// See the internal ui full plan.
export const load = async () => { throw redirect(303, '/hub'); };
