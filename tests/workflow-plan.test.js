import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { Script, createContext } from 'node:vm';
import { performance } from 'node:perf_hooks';

const source = await readFile(new URL('../src/workflow-plan.js', import.meta.url), 'utf8');
const context = createContext({ performance, Promise, Object, Set, Map, Error, Math });
new Script(source).runInContext(context);
const executeWorkflowPlan = context.executeWorkflowPlan;

function measuredStep(id, state, waitMs, dependsOn = []) {
  return {
    id,
    dependsOn,
    run: async ({ results }) => {
      state.active += 1;
      state.peak = Math.max(state.peak, state.active);
      try {
        await new Promise(resolve => setTimeout(resolve, waitMs));
        if (id === 'join') {
          assert.equal(results.first, 'first');
          assert.equal(results.second, 'second');
        }
        return id;
      } finally {
        state.active -= 1;
      }
    },
  };
}

test('planned mode overlaps independent steps and waits for dependencies', async () => {
  const state = { active: 0, peak: 0 };
  const plan = [
    measuredStep('first', state, 35),
    measuredStep('second', state, 35),
    measuredStep('join', state, 1, ['first', 'second']),
  ];

  const output = await executeWorkflowPlan(plan, { mode: 'planned', maxConcurrency: 2 });
  assert.equal(state.peak, 2);
  assert.deepEqual(
    { first: output.results.first, second: output.results.second, join: output.results.join },
    { first: 'first', second: 'second', join: 'join' },
  );
  assert.ok(output.timings.join.started_ms >= output.timings.first.ended_ms);
  assert.ok(output.timings.join.started_ms >= output.timings.second.ended_ms);
});

test('sequential mode never overlaps ready steps', async () => {
  const state = { active: 0, peak: 0 };
  const plan = [measuredStep('first', state, 15), measuredStep('second', state, 15)];

  await executeWorkflowPlan(plan, { mode: 'sequential', maxConcurrency: 4 });
  assert.equal(state.peak, 1);
});

test('rejects duplicate ids, unknown dependencies, and cycles', async () => {
  const noop = async () => 'ok';
  await assert.rejects(
    executeWorkflowPlan([{ id: 'same', run: noop }, { id: 'same', run: noop }]),
    /plan is invalid/,
  );
  await assert.rejects(
    executeWorkflowPlan([{ id: 'one', dependsOn: ['missing'], run: noop }]),
    /unknown dependency/,
  );
  await assert.rejects(
    executeWorkflowPlan([
      { id: 'one', dependsOn: ['two'], run: noop },
      { id: 'two', dependsOn: ['one'], run: noop },
    ]),
    /dependency cycle/,
  );
});
