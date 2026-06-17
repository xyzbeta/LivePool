(function(){
  var m = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', m);
  window.toggleTheme = function(){
    m = m === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', m);
    localStorage.setItem('theme', m);
  };
})();
