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

test('A/B: planned search returns the same matches faster than the sequential baseline', async () => {
  async function run(mode) {
    const state = { active: 0, peak: 0 };
    const sourceStep = (id, waitMs) => ({
      id,
      run: async () => {
        state.active += 1;
        state.peak = Math.max(state.peak, state.active);
        try {
          await new Promise(resolve => setTimeout(resolve, waitMs));
          return ['role-a', 'role-b'];
        } finally {
          state.active -= 1;
        }
      },
    });
    const plan = [
      sourceStep('boards', 90),
      sourceStep('index', 105),
      {
        id: 'rank',
        dependsOn: ['boards', 'index'],
        run: async ({ results }) => [...new Set([...results.boards, ...results.index])].sort(),
      },
    ];
    const started = performance.now();
    const execution = await executeWorkflowPlan(plan, { mode, maxConcurrency: 2 });
    return {
      matches: execution.results.rank,
      wallMs: Math.round(performance.now() - started),
      peak: state.peak,
    };
  }

  const planned = await run('planned');
  const sequential = await run('sequential');
  assert.deepEqual(planned.matches, sequential.matches);
  assert.equal(planned.peak, 2);
  assert.equal(sequential.peak, 1);
  assert.ok(planned.wallMs < sequential.wallMs, `${planned.wallMs}ms should beat ${sequential.wallMs}ms`);
  console.log(`Synthetic search A/B: planned ${planned.wallMs}ms, sequential ${sequential.wallMs}ms; both returned ${planned.matches.length} identical matches.`);
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
