// Evaluate the workflow's cron expression with the same `cron` library n8n's Schedule Trigger schedules with.
const { CronTime } = require(process.argv[2]);
const workflow = JSON.parse(require('fs').readFileSync('/data/workflow.json', 'utf8'));
const trigger = workflow.nodes.find((n) => n.type === 'n8n-nodes-base.scheduleTrigger');
const expression = trigger.parameters.rule.interval[0].expression;
const zone = workflow.settings.timezone;
const time = new CronTime(expression, zone);
let at = time.getNextDateFrom(new Date('2026-10-26T00:00:00Z'));
const fires = [];
for (let i = 0; i < 6; i++) {
  fires.push(at.toUTC().toISO());
  at = time.getNextDateFrom(new Date(at.toMillis() + 1000));
}
console.log('expression ' + expression + ' in ' + zone);
console.log('next six fires: ' + fires.join(' | '));
