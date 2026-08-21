document.addEventListener('DOMContentLoaded', () => {
    // ----- Data store -----
    const state = {
        gender: null,
        age: null,
        track: null,          // 'past', 'present', 'future'
        goal: null,
        goalOther: '',
        // answers will be collected from form inputs
    };

    // ----- DOM refs -----
    const steps = {
        landing: document.getElementById('step-landing'),
        goal: document.getElementById('step-goal'),
        questions: document.getElementById('step-questions'),
        result: document.getElementById('step-result'),
    };

    const genderContainer = document.getElementById('gender-options');
    const ageContainer = document.getElementById('age-options');
    const trackBtns = document.querySelectorAll('.track-btn');
    const goalOptionsContainer = document.getElementById('goal-options');
    const goalOtherContainer = document.getElementById('goal-other-container');
    const goalOtherInput = document.getElementById('goal-other-input');
    const goalBackBtn = document.getElementById('goal-back-btn');
    const goalNextBtn = document.getElementById('goal-next-btn');
    const questionsBackBtn = document.getElementById('questions-back-btn');
    const submitBtn = document.getElementById('submit-btn');
    const restartBtn = document.getElementById('restart-btn');
    const resultContent = document.getElementById('result-content');
    const questionsContainer = document.getElementById('questions-container');
    const form = document.getElementById('questionnaire-form');

    // Track-specific data
    const trackData = {
        past: {
            title: 'Прошлое',
            questions: [
                { id: 'event_name', label: 'Озаглавьте это воспоминание одним-двумя словами (как фильм).', hint: 'Пример: «Выпускной», «Та ночь», «Разговор с отцом»', type: 'text' },
                { id: 'age_at_moment', label: 'Возраст в моменте: Сколько вам тогда было лет?', hint: '', type: 'text' },
                { id: 'place_action', label: 'Место действия — глагол + локация: Опишите, ГДЕ это было, и что вы ДЕЛАЛИ в первых секундах.', hint: 'Пример: «Входил в пустой класс», «Сидел на подоконнике», «Бежал через двор»', type: 'text' },
                { id: 'light_weather', label: 'Свет и погода в той сцене — одним словом или цветом.', hint: '', type: 'choice', options: ['солнце', 'сумерки', 'лампа', 'дождь', 'туман', 'другое'] },
            ],
            goals: ['отпустить', 'переосмыслить', 'показать ребёнку', 'прожить иначе', 'другое']
        },
        present: {
            title: 'Настоящее',
            questions: [
                { id: 'present_role', label: 'Моя роль сегодня (выберите одну или придумайте):', hint: '', type: 'choice', options: ['Отец', 'мать', 'друг', 'одиночка', 'борец', 'творец', 'спасатель', 'другое'] },
                { id: 'present_place', label: 'Место силы: Где вы чувствуете себя «в своей тарелке» прямо сейчас?', hint: 'Пример: кухня в 6 утра, спортзал, машина перед домом, парк', type: 'text' },
                { id: 'time_day', label: 'Время суток, которое отражает ваш ритм:', hint: '', type: 'choice', options: ['утро', 'день', 'вечер', 'ночь'] },
                { id: 'one_thing', label: 'Одна вещь, которая у вас всегда с собой (в кадре)', hint: 'Пример: наушники, кольцо, трекер, кружка, блокнот', type: 'text' },
            ],
            goals: ['принять решение', 'увидеть свою роль', 'осознать ценности', 'другое']
        },
        future: {
            title: 'Будущее',
            questions: [
                { id: 'future_date', label: 'Когда это происходит?', hint: '', type: 'choice', options: ['через полгода', 'через год', 'через три года', 'через пять лет'] },
                { id: 'future_role', label: 'Кто вы в этом будущем — новая роль?', hint: 'Пример: предприниматель, свободный художник, родитель, наставник', type: 'text' },
                { id: 'future_place', label: 'Локация будущего — где вы физически находитесь?', hint: 'Пример: свой дом, другой город, студия, поезд', type: 'text' },
                { id: 'future_action', label: 'Одно действие, которое вы точно делаете в этом видео (глагол)', hint: 'Пример: открываю дверь, подписываю документ, смотрю в окно', type: 'text' },
            ],
            goals: ['мотивация', 'визуализация цели', 'преодолеть страх выбора', 'другое']
        }
    };

    // ----- Helper: show a step -----
    function showStep(stepId) {
        Object.keys(steps).forEach(key => {
            steps[key].classList.toggle('active', key === stepId);
        });
    }

    // ----- Step 1: Gender & Age selection -----
    function setupOptionButtons(container, stateKey) {
        const btns = container.querySelectorAll('button');
        btns.forEach(btn => {
            btn.addEventListener('click', () => {
                btns.forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
                state[stateKey] = btn.dataset.value;
                // Enable track buttons only if both gender and age are selected
                const genderSelected = state.gender !== null;
                const ageSelected = state.age !== null;
                trackBtns.forEach(tb => tb.disabled = !(genderSelected && ageSelected));
            });
        });
    }
    setupOptionButtons(genderContainer, 'gender');
    setupOptionButtons(ageContainer, 'age');

    // Initially disable track buttons
    trackBtns.forEach(tb => tb.disabled = true);

    // ----- Track selection -> go to goal step -----
    trackBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const track = btn.dataset.track;
            state.track = track;
            // Populate goal options
            const goals = trackData[track].goals;
            goalOptionsContainer.innerHTML = '';
            goals.forEach(g => {
                const b = document.createElement('button');
                b.type = 'button';
                b.textContent = g;
                b.dataset.value = g;
                b.addEventListener('click', () => {
                    // Deselect others
                    goalOptionsContainer.querySelectorAll('button').forEach(b2 => b2.classList.remove('selected'));
                    b.classList.add('selected');
                    state.goal = g;
                    // Show "other" input if needed
                    if (g === 'другое') {
                        goalOtherContainer.style.display = 'block';
                        goalOtherInput.focus();
                    } else {
                        goalOtherContainer.style.display = 'none';
                        state.goalOther = '';
                    }
                    goalNextBtn.disabled = false;
                });
                goalOptionsContainer.appendChild(b);
            });
            // Reset selection
            state.goal = null;
            state.goalOther = '';
            goalNextBtn.disabled = true;
            goalOtherContainer.style.display = 'none';
            goalOtherInput.value = '';
            document.getElementById('goal-track-title').textContent = `Вы выбрали: ${trackData[track].title}`;
            showStep('goal');
        });
    });

    // "Other" input for goal
    goalOtherInput.addEventListener('input', () => {
        state.goalOther = goalOtherInput.value.trim();
        // Optionally auto-enable next if non-empty
        if (state.goal === 'другое' && state.goalOther.length > 0) {
            goalNextBtn.disabled = false;
        } else if (state.goal === 'другое') {
            goalNextBtn.disabled = true;
        }
    });

    // Goal navigation
    goalBackBtn.addEventListener('click', () => {
        showStep('landing');
    });

    goalNextBtn.addEventListener('click', () => {
        // If goal is "other", ensure text is filled
        if (state.goal === 'другое' && state.goalOther.length === 0) {
            alert('Пожалуйста, укажите вашу цель в поле "Другое".');
            return;
        }
        // Build question form
        buildQuestionForm();
        showStep('questions');
    });

    // ----- Build question form based on track -----
    function buildQuestionForm() {
        const track = state.track;
        const data = trackData[track];
        document.getElementById('questions-title').textContent = `Вопросы по теме: ${data.title}`;

        questionsContainer.innerHTML = '';
        // Add questions
        data.questions.forEach((q, index) => {
            const div = document.createElement('div');
            div.className = 'question-block';

            const label = document.createElement('label');
            label.textContent = q.label;
            div.appendChild(label);

            if (q.hint) {
                const hint = document.createElement('div');
                hint.className = 'hint';
                hint.textContent = q.hint;
                div.appendChild(hint);
            }

            if (q.type === 'text') {
                const input = document.createElement('input');
                input.type = 'text';
                input.name = q.id;
                input.placeholder = 'Ваш ответ...';
                div.appendChild(input);
            } else if (q.type === 'choice') {
                // Hidden input carries the selected value into FormData on submit
                const hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = q.id;
                div.appendChild(hidden);

                const choiceContainer = document.createElement('div');
                choiceContainer.className = 'options-row';
                q.options.forEach(opt => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.textContent = opt;
                    btn.dataset.value = opt;
                    btn.addEventListener('click', () => {
                        choiceContainer.querySelectorAll('button').forEach(b => b.classList.remove('selected'));
                        btn.classList.add('selected');
                        hidden.value = opt;
                        // If "другое", show text input
                        const otherInput = div.querySelector('.other-input');
                        if (opt === 'другое') {
                            otherInput.style.display = 'block';
                            otherInput.focus();
                        } else {
                            otherInput.style.display = 'none';
                            otherInput.value = '';
                        }
                    });
                    choiceContainer.appendChild(btn);
                });
                div.appendChild(choiceContainer);
                // "Other" text input
                const otherInput = document.createElement('input');
                otherInput.type = 'text';
                otherInput.className = 'other-input';
                otherInput.placeholder = 'Укажите свой вариант';
                otherInput.style.display = 'none';
                otherInput.name = q.id + '_other';
                div.appendChild(otherInput);
            }

            questionsContainer.appendChild(div);
        });

        // Add file upload field (common for all tracks)
        const fileDiv = document.createElement('div');
        fileDiv.className = 'question-block';
        const fileLabel = document.createElement('label');
        fileLabel.textContent = 'Вы можете загрузить файл с воспоминанием, это очень поможет мне';
        fileDiv.appendChild(fileLabel);
        const uploadDiv = document.createElement('div');
        uploadDiv.className = 'file-upload';
        const label = document.createElement('label');
        label.textContent = '📎 Выберите файл';
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.name = 'memory_file';
        fileInput.accept = 'image/*,video/*,audio/*,.txt,.pdf';
        label.appendChild(fileInput);
        uploadDiv.appendChild(label);
        const fileNameSpan = document.createElement('span');
        fileNameSpan.className = 'file-name';
        fileNameSpan.textContent = 'Файл не выбран';
        uploadDiv.appendChild(fileNameSpan);
        fileInput.addEventListener('change', (e) => {
            const file = e.target.files[0];
            fileNameSpan.textContent = file ? file.name : 'Файл не выбран';
        });
        fileDiv.appendChild(uploadDiv);
        questionsContainer.appendChild(fileDiv);
    }

    // ----- Questions navigation -----
    questionsBackBtn.addEventListener('click', () => {
        showStep('goal');
    });

    // ----- Form submission -----
    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        // Validate text answers
        for (const inp of questionsContainer.querySelectorAll('input[type="text"]:not(.other-input)')) {
            if (!inp.value.trim()) {
                alert('Пожалуйста, заполните все поля ответов.');
                inp.focus();
                return;
            }
        }

        // Validate choice selections
        for (const block of questionsContainer.querySelectorAll('.question-block')) {
            const selectedBtn = block.querySelector('.options-row button.selected');
            if (block.querySelector('.options-row') && !selectedBtn) {
                alert('Пожалуйста, выберите вариант в каждом вопросе.');
                return;
            }
            if (selectedBtn && selectedBtn.dataset.value === 'другое') {
                const otherInput = block.querySelector('.other-input');
                if (!otherInput || !otherInput.value.trim()) {
                    alert('Пожалуйста, уточните свой вариант.');
                    if (otherInput) otherInput.focus();
                    return;
                }
            }
        }

        // Text inputs, hidden choice inputs and the file are collected by FormData(form)
        const formData = new FormData(form);

        // Add gender, age, track, goal
        formData.append('gender', state.gender);
        formData.append('age', state.age);
        formData.append('track', state.track);
        formData.append('goal', state.goal);
        if (state.goal === 'другое') {
            formData.append('goal_other', state.goalOther);
        }

        // Disable submit button
        submitBtn.disabled = true;
        submitBtn.textContent = 'Отправка...';

        try {
            const response = await fetch('/submit_questionnaire', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                let detail = `Ошибка ${response.status}`;
                try {
                    const err = await response.json();
                    detail = err.detail || detail;
                } catch (_) {
                    // non-JSON error body (e.g. HTML from a proxy)
                }
                throw new Error(detail);
            }

            const data = await response.json();
            resultContent.textContent = data.response || 'Ответ от нейросети не получен.';
            showStep('result');

        } catch (err) {
            alert('Ошибка: ' + err.message);
        } finally {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Отправить';
        }
    });

    // ----- Restart -----
    restartBtn.addEventListener('click', () => {
        // Reset state
        state.gender = null;
        state.age = null;
        state.track = null;
        state.goal = null;
        state.goalOther = '';
        // Reset UI selections
        document.querySelectorAll('.options-row button, .options-list button').forEach(b => b.classList.remove('selected'));
        document.querySelectorAll('.track-btn').forEach(b => b.disabled = true);
        document.querySelectorAll('input[type="text"]').forEach(inp => inp.value = '');
        document.querySelectorAll('.other-input').forEach(inp => inp.style.display = 'none');
        document.querySelectorAll('.file-name').forEach(el => el.textContent = 'Файл не выбран');
        // Reset form
        form.reset();
        showStep('landing');
    });

    // Show landing initially
    showStep('landing');
});