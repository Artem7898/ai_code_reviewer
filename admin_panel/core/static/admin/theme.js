document.addEventListener("DOMContentLoaded", function() {
    // Создаем кнопку переключения тем
    const btn = document.createElement('button');
    btn.innerHTML = '🎨';
    btn.title = "Сменить тему";
    btn.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        width: 50px;
        height: 50px;
        border-radius: 50%;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border: none;
        box-shadow: 0 4px 10px rgba(0,0,0,0.3);
        cursor: pointer;
        z-index: 9999;
        font-size: 24px;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: transform 0.2s;
    `;

    // Анимация при наведении
    btn.onmouseover = () => btn.style.transform = "scale(1.1)";
    btn.onmouseout = () => btn.style.transform = "scale(1)";

    // Меню выбора цветов
    let isMenuOpen = false;
    const colors = {
        'purple': 'rgba(99, 102, 241, 1)',  // Original Indigo
        'blue': 'rgba(59, 130, 246, 1)',   // Blue
        'green': 'rgba(16, 185, 129, 1)',  // Green
        'orange': 'rgba(249, 115, 22, 1)', // Orange
        'red': 'rgba(239, 68, 68, 1)'      // Red
    };

    btn.onclick = function(e) {
        e.stopPropagation();
        isMenuOpen = !isMenuOpen;

        // Удаляем старое меню если есть
        const oldMenu = document.getElementById('theme-menu');
        if (oldMenu) oldMenu.remove();

        if (isMenuOpen) {
            const menu = document.createElement('div');
            menu.id = 'theme-menu';
            menu.style.cssText = `
                position: fixed;
                bottom: 80px;
                right: 20px;
                background: white;
                padding: 10px;
                border-radius: 12px;
                box-shadow: 0 5px 15px rgba(0,0,0,0.2);
                z-index: 9998;
                display: flex;
                flex-direction: column;
                gap: 8px;
            `;

            Object.entries(colors).forEach(([name, color]) => {
                const colorBtn = document.createElement('div');
                colorBtn.style.cssText = `
                    width: 30px; height: 30px; border-radius: 50%;
                    background: ${color}; cursor: pointer;
                    border: 2px solid #e5e7eb;
                `;
                colorBtn.title = name;
                colorBtn.onclick = () => {
                    applyTheme(color);
                    isMenuOpen = false;
                    menu.remove();
                    localStorage.setItem('admin_theme_color', color);
                };
                menu.appendChild(colorBtn);
            });
            document.body.appendChild(menu);
        }
    };

    // Применяем тему при загрузке
    const savedColor = localStorage.getItem('admin_theme_color') || colors['purple'];
    if (savedColor) applyTheme(savedColor);

    document.body.appendChild(btn);
});

function applyTheme(color) {
    // Unfold и Tailwind используют CSS переменные или специфические классы.
    // Мы просто перезапишем все основные цвета элементов через JS inject.

    // Более надежный способ для Tailwind/Unfold без перезагрузки:
    // 1. Найти элементы с основными классами и заменить их inline стили или классы
    // Но самый простой способ - инжектить CSS переменные, если Unfold их поддерживает,
    // или использовать filter: hue-rotate для радикальной смены.

    // Для прототипа будем менять стиль кнопок и хедера прямо через JS
    document.querySelectorAll('.bg-indigo-600, .bg-primary-600').forEach(el => {
        el.style.backgroundColor = color;
    });

    // Если нужно точнее, используем hue-rotate для всей страницы
    // Но для точных цветов лучше менять переменные, если они доступны.

    // Простой костыль для демонстрации (меняем кнопку админа и хедер)
    const mainHeader = document.querySelector('header');
    if (mainHeader) mainHeader.style.backgroundColor = color;

    // Меняем кнопки действий
    const actionBtns = document.querySelectorAll('.button-primary, .bg-indigo-500');
    actionBtns.forEach(btn => {
        btn.style.backgroundColor = color;
    });
}