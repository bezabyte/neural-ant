globalThis.executeWorkflowPlan = async function executeWorkflowPlan(
  plan,
  { mode = 'planned', maxConcurrency = 4 } = {},
) {
  const ids = new Set();
  for (const step of plan) {
    if (!step?.id || ids.has(step.id) || typeof step.run !== 'function') {
      throw Error('The prepared workflow plan is invalid.');
    }
    ids.add(step.id);
  }

  for (const step of plan) {
    for (const dependency of step.dependsOn || []) {
      if (!ids.has(dependency)) throw Error('The prepared workflow plan has an unknown dependency.');
    }
  }

  const pending = new Map(plan.map(step => [step.id, step]));
  const results = {};
  const timings = {};
  const started = performance.now();

  while (pending.size) {
    const ready = [...pending.values()].filter(step =>
      (step.dependsOn || []).every(id => Object.hasOwn(results, id)),
    );
    if (!ready.length) throw Error('The prepared workflow plan contains a dependency cycle.');

    const group = mode === 'planned' ? ready.slice(0, Math.max(1, maxConcurrency)) : ready.slice(0, 1);
    const errors = [];
    await Promise.all(group.map(async step => {
      const stepStarted = performance.now();
      try {
        results[step.id] = await step.run({ results, mode });
      } catch (error) {
        errors.push(error);
      } finally {
        const ended = performance.now();
        timings[step.id] = {
          started_ms: Math.round(stepStarted - started),
          ended_ms: Math.round(ended - started),
          duration_ms: Math.round(ended - stepStarted),
        };
        pending.delete(step.id);
      }
    }));
    if (errors.length) throw errors[0];
  }

  return { results, timings };
};
