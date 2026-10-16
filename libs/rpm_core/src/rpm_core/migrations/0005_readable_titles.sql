-- Alerts and patient replies stored before 16 Oct 2026 named internal codes ("CHECKIN_REPLY — Check-in reply: 'Yes'
-- to breathless-d4e7e415", "RISK_HIGH — …", "Yes (to breathless-d4e7e415)"). Reword them as the code now writes them.
CREATE TEMP TABLE _checkin_topic (topic text PRIMARY KEY, about text) ON COMMIT DROP;
INSERT INTO _checkin_topic VALUES
    ('breathless', 'more short of breath than usual'),
    ('chest_pain', 'chest pain, pressure or tightness'),
    ('dizzy', 'dizzy, faint, confused or heart racing'),
    ('fever', 'feverish, shivery or chills'),
    ('readings', 'home blood pressure and temperature measured');

UPDATE alerts a SET title = 'Symptoms reported — Patient answered '
        || initcap(substring(a.title from '''([^'']*)''')) || ' to “' || t.about || '”'
    FROM _checkin_topic t
    WHERE a.title LIKE 'CHECKIN_REPLY — %' AND substring(a.title from ' to ([a-z_]+)-') = t.topic;
UPDATE alerts SET title = 'Risk model — ' || substr(title, length('RISK_HIGH — ') + 1) WHERE title LIKE 'RISK_HIGH — %';
UPDATE alerts SET title = 'NEWS2 rising — ' || substr(title, length('NEWS2_RISING — ') + 1)
    WHERE title LIKE 'NEWS2_RISING — %';

UPDATE messages m SET text = initcap(substring(m.text from '^(\S+) \(to ')) || ' to “' || t.about || '”'
    FROM _checkin_topic t
    WHERE m.direction = 'from_patient' AND m.text ~ '^\S+ \(to [a-z_]+-[^)]*\)$'
      AND substring(m.text from '\(to ([a-z_]+)-') = t.topic;
